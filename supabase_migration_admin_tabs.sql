-- ============================================================
-- 管理后台双标签页 RPC 函数
-- 为 article_reports 和 service_requests 两个表创建管理员查询函数
-- 执行方式：Supabase Dashboard → SQL Editor → 粘贴执行
-- ============================================================

-- 1. 智能选文报告查询函数
create or replace function get_article_reports(admin_pwd text)
returns setof article_reports
language plpgsql
security definer
set search_path = public
as $$
begin
    if admin_pwd is distinct from 'Clarins202020' then
        raise exception 'unauthorized';
    end if;
    return query select * from article_reports order by created_at desc;
end;
$$;

-- 2. 定制化深度报告查询函数
create or replace function get_service_requests(admin_pwd text)
returns setof service_requests
language plpgsql
security definer
set search_path = public
as $$
begin
    if admin_pwd is distinct from 'Clarins202020' then
        raise exception 'unauthorized';
    end if;
    return query select * from service_requests order by created_at desc;
end;
$$;

-- 3. 确保两个表 RLS 已开启且匿名只能写入
alter table article_reports enable row level security;
alter table service_requests enable row level security;

-- 4. 匿名写入策略（如果尚未创建）
do $$
begin
    if not exists (
        select 1 from pg_policies 
        where tablename = 'article_reports' and policyname = 'anyone_can_submit_article_report'
    ) then
        create policy "anyone_can_submit_article_report"
            on article_reports for insert
            to anon
            with check (true);
    end if;

    if not exists (
        select 1 from pg_policies 
        where tablename = 'service_requests' and policyname = 'anyone_can_submit_service_request'
    ) then
        create policy "anyone_can_submit_service_request"
            on service_requests for insert
            to anon
            with check (true);
    end if;
end $$;
