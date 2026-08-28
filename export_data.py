#!/usr/bin/env python3
"""
化妆品文章数据导出脚本
用于定时更新网站数据
"""

import sqlite3
import json
import os
import re
from datetime import datetime

DB_PATH = os.path.expanduser('~/.openclaw/workspace/cosmetic-deploy/cosmetic_articles.db')
OUTPUT_PATH = os.path.expanduser('~/.openclaw/workspace/cosmetic-deploy/data.json')
OUTPUT_DIR = os.path.dirname(OUTPUT_PATH)

def get_next_version():
    """扫描目录下已有的 data.vXX.json，取最大编号+1作为新版本号"""
    max_version = 0
    pattern = re.compile(r'^data\.v(\d+)\.json$')
    for fname in os.listdir(OUTPUT_DIR):
        m = pattern.match(fname)
        if m:
            num = int(m.group(1))
            if num > max_version:
                max_version = num
    return f'v{max_version + 1}'


def export_data():
    """导出数据库数据为JSON"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 获取统计信息
        cursor.execute("SELECT COUNT(*) FROM articles")
        total = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(DISTINCT wechat_name) FROM articles")
        source_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT MIN(publish_date), MAX(publish_date) FROM articles")
        date_range = cursor.fetchone()
        
        # 获取公众号列表
        cursor.execute("""
            SELECT wechat_name as source, COUNT(*) as count 
            FROM articles 
            GROUP BY wechat_name 
            ORDER BY count DESC
        """)
        sources = []
        for row in cursor.fetchall():
            sources.append({
                'name': row['source'],
                'count': row['count']
            })
        
        # 获取所有文章（按公众号分组）
        articles_by_source = {}
        for source in sources:
            source_name = source['name']
            cursor.execute("""
                SELECT id, title, wechat_name as source, publish_date, url, content as summary, keywords, images_json
                FROM articles
                WHERE wechat_name = ?
                ORDER BY publish_date DESC
            """, (source_name,))
            
            articles = []
            for row in cursor.fetchall():
                try:
                    keywords = json.loads(row['keywords']) if row['keywords'] else []
                except:
                    keywords = []
                
                cover = ''
                try:
                    imgs = json.loads(row['images_json']) if row['images_json'] else []
                    if imgs:
                        cover = imgs[0]
                except:
                    pass
                
                articles.append({
                    'id': row['id'],
                    'title': row['title'],
                    'source': row['source'],
                    'publish_date': row['publish_date'],
                    'url': row['url'],
                    'summary': row['summary'] or '',
                    'keywords': keywords,
                    'cover': cover
                })
            
            articles_by_source[source_name] = articles
        
        conn.close()
        
        # 构建数据（版本号自动递增）
        version = get_next_version()
        data = {
            'version': version,
            'stats': {
                'total_articles': total,
                'source_count': source_count,
                'date_range': {
                    'start': date_range[0],
                    'end': date_range[1]
                },
                'last_update': datetime.now().isoformat()
            },
            'sources': sources,
            'articles': articles_by_source
        }
        
        # 保存为JSON（主文件）
        with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # 保存为版本号文件 data.vXX.json
        versioned_path = os.path.join(OUTPUT_DIR, f'data.{version}.json')
        with open(versioned_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # ===== 同步生成 stats.json（首页统计专用小文件） =====
        all_articles_flat = []
        for source_name, items in articles_by_source.items():
            for item in items:
                item['source'] = source_name
                all_articles_flat.append(item)
        all_articles_flat.sort(key=lambda x: x.get('publish_date', ''), reverse=True)
        latest5 = all_articles_flat[:5]

        stats_data = {
            'stats': data['stats'],
            'latest': latest5
        }
        stats_path = os.path.join(OUTPUT_DIR, 'stats.json')
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats_data, f, ensure_ascii=False, indent=2)

        # ===== 自动更新前端HTML中的数据版本号 =====
        today_str = datetime.now().strftime('%Y%m%d')
        html_files = ['index.html', 'articles.html', 'report.html', 'service.html', 'submit.html', 'find.html', 'admin.html']
        for html_file in html_files:
            html_path = os.path.join(OUTPUT_DIR, html_file)
            if not os.path.exists(html_path):
                continue
            with open(html_path, 'r', encoding='utf-8') as f:
                content = f.read()
            original = content
            # 更新 stats.json?v=YYYYMMDD
            content = re.sub(r"stats\.json\?v=\d{8}", f'stats.json?v={today_str}', content)
            # 更新 data.vXX.json 引用为最新版本
            content = re.sub(r"data\.v\d+\.json", f'data.{version}.json', content)
            # 更新 meta data-version 标签（版本检测脚本用）
            content = re.sub(r'name="data-version" content="v\d+"', f'name="data-version" content="{version}"', content)
            if content != original:
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"   📄 {html_file}: 版本号已更新")

        print(f"✅ 数据导出成功: {OUTPUT_PATH}")
        print(f"   版本号: {version} ({versioned_path})")
        print(f"   stats.json: {stats_path}")
        print(f"   文章总数: {total}")
        print(f"   公众号数: {source_count}")
        print(f"   更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        return True
        
    except Exception as e:
        print(f"❌ 导出失败: {e}")
        return False

if __name__ == '__main__':
    export_data()
