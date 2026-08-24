from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict
import sqlite3
import re
import json
import subprocess
from datetime import datetime, timedelta
import os

app = FastAPI(title="化妆品文章智能检索API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "/Users/yuming.chen/.openclaw/workspace/cosmetic-deploy/cosmetic_articles.db"

class Article(BaseModel):
    id: int
    title: str
    url: str
    wechat_name: str
    publish_date: str
    summary: Optional[str] = None
    keywords: Optional[str] = None
    match_reason: Optional[str] = None
    score: Optional[float] = None

class SearchRequest(BaseModel):
    query: str
    limit: int = 50
    offset: int = 0

class SearchResponse(BaseModel):
    total: int
    articles: List[Article]
    query: str
    took_ms: int
    keywords: Optional[Dict[str, List[str]]] = None

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ============ 时间过滤 ============

def parse_time_filter(query: str) -> Optional[str]:
    """从查询中提取时间限定词，返回对应的日期过滤条件（publish_date >= date）"""
    today = datetime.now().date()
    
    time_patterns = [
        (r'近一?周|最近一?周|本周|这周|这一周', 7),
        (r'近两周|最近两周', 14),
        (r'近一?个?月|最近一?个?月|本月|这个月', 30),
        (r'近三[天日]|最近三[天日]|三[天日]内', 3),
        (r'近五[天日]|最近五[天日]|五[天日]内', 5),
        (r'近十[天日]|最近十[天日]|十[天日]内', 10),
        (r'近半[个]?月|最近半[个]?月', 15),
        (r'今年|本年', 365),
        (r'近期|最近|最新|近来', 30),
    ]
    
    for pattern, days in time_patterns:
        if re.search(pattern, query):
            cutoff = today - timedelta(days=days)
            print(f"时间过滤: '{query}' 匹配 '{pattern}' → 近{days}天 → >= {cutoff}")
            return cutoff.isoformat()
    
    return None

# ============ 步骤1：LLM意图解析（锚点+修饰词） ============

def llm_parse_intent(query: str) -> Dict:
    """用LLM解析用户查询的核心意图锚点和修饰概念"""
    
    prompt = f"""用户搜索: "{query}"

请分析这个搜索查询的意图结构，输出以下JSON：

1. **anchor**: 查询的核心概念——用户真正想找的主题。这是检索的锚点，文章必须主要讨论这个概念。
   - 例如"抗衰老成分趋势"的anchor是"抗衰老"（不是"成分"也不是"趋势"）
   - 例如"美白新品推荐"的anchor是"美白"
   - 例如"玻尿酸保湿效果"的anchor是"玻尿酸"

2. **anchor_synonyms**: anchor的同义词、近义词、相关术语（5-10个），用于扩大召回
   - 例如anchor="抗衰老"时，同义词包括：抗老、抗皱、紧致、提升、年轻化、冻龄、逆龄

3. **modifiers**: 查询中的修饰概念（1-5个），用于排序加权但不作为必须条件
   - 例如"抗衰老成分趋势"的modifiers是["成分", "趋势"]

4. **exclude_if**: 如果文章主要讨论这些主题，即使提及anchor也应排除（3-5个）
   - 例如anchor="抗衰老"时，exclude_if可以是["美白为主", "保湿为主", "祛痘为主", "节日祝福", "企业新闻"]

请严格按以下JSON格式返回（不要其他内容）：
{{
  "anchor": "核心概念",
  "anchor_synonyms": ["同义词1", "同义词2", ...],
  "modifiers": ["修饰词1", "修饰词2", ...],
  "exclude_if": ["排除主题1", "排除主题2", ...]
}}"""

    try:
        cmd = [
            "/Users/yuming.chen/.nvm/versions/node/v22.22.2/bin/node",
            "/Users/yuming.chen/.nvm/versions/node/v22.22.2/bin/openclaw",
            "agent", "--local", "--agent", "main",
            "--model", "kimi/kimi-k3",
            "--message", prompt,
            "--json", "--timeout", "60"
        ]
        
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=90,
            env={**os.environ, "PATH": "/Users/yuming.chen/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"}
        )
        
        if result.returncode != 0:
            print(f"LLM意图解析失败: {result.stderr[:200]}")
            return fallback_intent(query)
        
        response = json.loads(result.stdout)
        text = ""
        if 'payloads' in response:
            for p in response['payloads']:
                if p.get('text'):
                    text = p['text']
                    break
        
        # 提取JSON
        json_match = re.search(r'\{[^{}]*"anchor"[^{}]*\}', text, re.DOTALL)
        if json_match:
            intent = json.loads(json_match.group(0))
            print(f"LLM意图解析: anchor={intent.get('anchor')}, synonyms={len(intent.get('anchor_synonyms',[]))}, modifiers={intent.get('modifiers')}")
            return intent
        
        return fallback_intent(query)
        
    except Exception as e:
        print(f"LLM意图解析异常: {e}")
        return fallback_intent(query)

def fallback_intent(query: str) -> Dict:
    """LLM失败时的简单意图解析"""
    import jieba
    words = [w for w in jieba.lcut(query) if len(w) > 1]
    anchor = words[0] if words else query
    return {
        'anchor': anchor,
        'anchor_synonyms': words[1:] if len(words) > 1 else [],
        'modifiers': [],
        'exclude_if': []
    }

# ============ 步骤2：锚点检索（AND逻辑） ============

def anchor_search(intent: Dict, limit: int = 50, date_from: str = None) -> List[dict]:
    """用意图锚点检索数据库——锚点必须命中，修饰词加权，支持时间过滤"""
    
    anchor = intent.get('anchor', '')
    anchor_synonyms = intent.get('anchor_synonyms', [])
    modifiers = intent.get('modifiers', [])
    
    # 锚点词组：anchor + 同义词
    anchor_terms = [anchor] + anchor_synonyms
    
    if not anchor_terms:
        return []
    
    conn = get_db()
    cursor = conn.cursor()
    
    # 构建FTS5查询：锚点词组AND，修饰词可选
    anchor_fts = ' OR '.join(f'"{t}"' for t in anchor_terms[:15])
    
    if modifiers:
        # 过滤掉时间相关的修饰词
        time_words = ['近一周', '最近一周', '本周', '这周', '近一个月', '最近一个月', '本月', '近期', '最近', '最新', '今年']
        non_time_modifiers = [m for m in modifiers if not any(tw in m for tw in time_words)]
        if non_time_modifiers:
            modifier_fts = ' OR '.join(f'"{t}"' for t in non_time_modifiers[:5])
            fts_query = f'({anchor_fts}) AND ({modifier_fts})'
        else:
            fts_query = f'({anchor_fts})'
    else:
        fts_query = f'({anchor_fts})'
    
    print(f"FTS5锚点查询: {fts_query[:200]}")
    
    # 时间过滤条件
    date_condition = ""
    params = [fts_query]
    if date_from:
        date_condition = "AND a.publish_date >= ?"
        params.append(date_from)
    params.append(limit)
    
    try:
        cursor.execute(f'''
            SELECT a.id, a.title, a.url, a.wechat_name, a.publish_date, a.summary, a.keywords,
                   bm25(articles_fts) as fts_score
            FROM articles a
            JOIN articles_fts ON a.id = articles_fts.rowid
            WHERE articles_fts MATCH ?
            {date_condition}
            ORDER BY fts_score
            LIMIT ?
        ''', params)
        
        results = cursor.fetchall()
        articles = [dict(row) for row in results]
        
        # 如果AND查询结果太少，回退到仅锚点查询
        if len(articles) < 5 and modifiers:
            print(f"AND查询结果太少({len(articles)}), 回退到仅锚点查询")
            fallback_params = [f'({anchor_fts})']
            if date_from:
                fallback_params.append(date_from)
            fallback_params.append(limit)
            cursor.execute(f'''
                SELECT a.id, a.title, a.url, a.wechat_name, a.publish_date, a.summary, a.keywords,
                       bm25(articles_fts) as fts_score
                FROM articles a
                JOIN articles_fts ON a.id = articles_fts.rowid
                WHERE articles_fts MATCH ?
                {date_condition}
                ORDER BY fts_score
                LIMIT ?
            ''', fallback_params)
            results = cursor.fetchall()
            articles = [dict(row) for row in results]
        
        return articles
        
    except sqlite3.OperationalError as e:
        print(f"FTS5查询失败: {e}, 回退到LIKE")
        anchor_conditions = []
        params = []
        for term in anchor_terms[:10]:
            anchor_conditions.append("(title LIKE ? OR summary LIKE ? OR keywords LIKE ?)")
            params.extend([f'%{term}%', f'%{term}%', f'%{term}%'])
        
        anchor_where = ' OR '.join(anchor_conditions)
        like_date = ""
        if date_from:
            like_date = "AND publish_date >= ?"
            params.append(date_from)
        params.append(limit)
        
        cursor.execute(f'''
            SELECT id, title, url, wechat_name, publish_date, summary, keywords
            FROM articles WHERE ({anchor_where})
            {like_date}
            ORDER BY publish_date DESC LIMIT ?
        ''', params)
        
        results = cursor.fetchall()
        articles = []
        for row in results:
            d = dict(row)
            d['fts_score'] = 1.0
            articles.append(d)
        return articles
    finally:
        conn.close()

# ============ 步骤3：LLM精排（意图锚定） ============

def llm_rerank(query: str, articles: List[dict], intent: Dict) -> List[dict]:
    """LLM基于意图锚点精排——判断文章是否主要讨论anchor概念"""
    
    if not articles:
        return articles
    
    anchor = intent.get('anchor', query)
    exclude_if = intent.get('exclude_if', [])
    
    # 构建文章列表（只取前20篇）
    articles_text = ""
    for i, article in enumerate(articles[:20], 1):
        summary = (article.get('summary') or '')[:300]
        articles_text += f"\n【文章{i}】ID:{article['id']}\n标题: {article['title']}\n摘要: {summary}\n"
    
    exclude_text = '\n'.join(f'- {e}' for e in exclude_if) if exclude_if else '- 无'
    
    prompt = f"""用户搜索: "{query}"
核心意图锚点: "{anchor}"

请判断以下每篇文章是否**主要讨论**"{anchor}"这个概念。

判断标准：
1. 文章的主题必须是"{anchor}"——不能只是简单提及
2. 如果文章主要讲其他主题，即使提到"{anchor}"，也应排除
3. 以下情况必须排除：
{exclude_text}

{articles_text}

请严格按以下JSON格式返回（不要其他内容）：
{{
  "selected": [
    {{"id": 文章ID, "relevance": 85, "reason": "这篇文章主要讨论{anchor}的具体原因"}},
    ...
  ]
}}

注意：
- relevance范围0-100，表示文章与"{anchor}"的相关程度
- 只保留relevance >= 60的文章
- 宁可少返回，也不要返回不相关的"""

    try:
        cmd = [
            "/Users/yuming.chen/.nvm/versions/node/v22.22.2/bin/node",
            "/Users/yuming.chen/.nvm/versions/node/v22.22.2/bin/openclaw",
            "agent", "--local", "--agent", "main",
            "--model", "kimi/kimi-k3",
            "--message", prompt,
            "--json", "--timeout", "120"
        ]
        
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=150,
            env={**os.environ, "PATH": "/Users/yuming.chen/.nvm/versions/node/v22.22.2/bin:/usr/local/bin:/usr/bin:/bin"}
        )
        
        if result.returncode != 0:
            print(f"LLM精排失败: {result.stderr[:200]}")
            for a in articles:
                a['llm_score'] = 50
                a['llm_reason'] = f'本文与"{anchor}"相关'
            return articles[:10]
        
        response = json.loads(result.stdout)
        text = ""
        if 'payloads' in response:
            for p in response['payloads']:
                if p.get('text'):
                    text = p['text']
                    break
        
        # 提取JSON
        json_match = re.search(r'\{[\s\S]*"selected"[\s\S]*\}', text)
        if json_match:
            try:
                llm_result = json.loads(json_match.group(0))
                selected_list = llm_result.get('selected', [])
                
                selected_ids = {r['id'] for r in selected_list}
                id_to_data = {r['id']: r for r in selected_list}
                
                filtered_articles = []
                for a in articles:
                    if a['id'] in selected_ids:
                        r = id_to_data[a['id']]
                        a['llm_reason'] = r.get('reason', f'本文主要讨论"{anchor}"')
                        a['llm_score'] = r.get('relevance', 70)
                        filtered_articles.append(a)
                
                # 按relevance排序
                filtered_articles.sort(key=lambda x: x.get('llm_score', 0), reverse=True)
                
                return filtered_articles[:10]
                
            except json.JSONDecodeError as e:
                print(f"LLM结果JSON解析失败: {e}")
        
        for a in articles:
            a['llm_score'] = 50
            a['llm_reason'] = f'本文与"{anchor}"相关'
        return articles[:10]
        
    except Exception as e:
        print(f"LLM精排异常: {e}")
        for a in articles:
            a['llm_score'] = 50
            a['llm_reason'] = f'本文与"{anchor}"相关'
        return articles[:10]

# ============ 匹配理由生成 ============

def generate_match_reason(article: dict, intent: Dict) -> str:
    """基于意图锚点生成匹配理由"""
    # 优先使用LLM的语义理由
    llm_reason = article.get('llm_reason', '')
    if llm_reason and '相关' not in llm_reason:
        return llm_reason
    
    anchor = intent.get('anchor', '')
    modifiers = intent.get('modifiers', [])
    
    text = f"{article['title']} {article.get('summary', '')} {article.get('keywords', '')}".lower()
    
    # 检查锚点匹配
    anchor_matched = anchor.lower() in text if anchor else False
    modifier_matched = [m for m in modifiers if m.lower() in text]
    
    if anchor_matched and modifier_matched:
        return f"本文主要讨论「{anchor}」，同时涉及「{'」「'.join(modifier_matched[:2])}」，与您的搜索高度匹配。"
    elif anchor_matched:
        return f"本文主要讨论「{anchor}」相关内容，与您的搜索意图匹配。"
    return f"本文涉及「{anchor}」相关内容。"

# ============ 搜索接口 ============

@app.post("/api/search", response_model=SearchResponse)
async def search_articles(request: SearchRequest):
    start_time = datetime.now()
    
    # 步骤1：LLM意图解析（锚点+修饰词）
    print(f"\n=== 搜索: {request.query} ===")
    intent = llm_parse_intent(request.query)
    print(f"意图锚点: {intent.get('anchor')}, 同义词: {len(intent.get('anchor_synonyms',[]))}, 修饰词: {intent.get('modifiers')}")
    
    # 步骤1.5：时间过滤解析
    date_from = parse_time_filter(request.query)
    
    # 步骤2：锚点检索（AND逻辑+时间过滤）
    articles = anchor_search(intent, limit=50, date_from=date_from)
    print(f"锚点检索召回: {len(articles)} 篇" + (f"（>= {date_from}）" if date_from else ""))
    
    # 步骤3：LLM精排（意图锚定）
    if len(articles) > 0:
        print(f"LLM精排: {len(articles)} 篇")
        articles = llm_rerank(request.query, articles, intent)
        print(f"LLM精排完成: {len(articles)} 篇")
    
    # 步骤4：生成匹配理由和相关度分数
    for article in articles:
        article['match_reason'] = generate_match_reason(article, intent)
        if 'llm_score' in article:
            article['score'] = article['llm_score']
        elif 'fts_score' in article:
            article['score'] = min(99, max(0, int(100 + article['fts_score'] * 10)))
        else:
            article['score'] = 50
    
    took_ms = int((datetime.now() - start_time).total_seconds() * 1000)
    
    # 构建keywords响应（兼容前端）
    keywords_response = {
        'anchor': [intent.get('anchor', '')],
        'anchor_synonyms': intent.get('anchor_synonyms', []),
        'modifiers': intent.get('modifiers', [])
    }
    
    return SearchResponse(
        total=len(articles),
        articles=[Article(**a) for a in articles],
        query=request.query,
        took_ms=took_ms,
        keywords=keywords_response
    )

@app.get("/api/articles/{article_id}", response_model=Article)
async def get_article(article_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, title, url, wechat_name, publish_date, summary, keywords FROM articles WHERE id = ?', (article_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="文章不存在")
    return Article(**dict(row))

@app.get("/api/stats")
async def get_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM articles')
    total = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(DISTINCT wechat_name) FROM articles')
    sources = cursor.fetchone()[0]
    cursor.execute('SELECT MIN(publish_date), MAX(publish_date) FROM articles')
    date_range = cursor.fetchone()
    conn.close()
    return {"total_articles": total, "total_sources": sources, "date_range": {"start": date_range[0], "end": date_range[1]}}

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
