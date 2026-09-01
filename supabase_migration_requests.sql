-- ============================================================
-- 提交收录需求（requests 表）RPC 函数
-- 为 requests 表创建管理员查询函数
-- 执行方式：Supabase Dashboard → SQL Editor → 粘贴执行
-- ============================================================

-- 提交收录需求查询函数
create or replace function get_requests(admin_pwd text)
returns setof requests
language plpgsql
security definer
set search_path = public
as $$
begin
    if admin_pwd is distinct from 'Clarins202020' then
        raise exception 'unauthorized';
    end if;
    return query select * from requests order by created_at desc;
end;
$$;

-- 确保 requests 表 RLS 已开启且匿名只能写入
alter table requests enable row level security;

-- 匿名写入策略（如果尚未创建）
do $$
begin
    if not exists (
        select 1 from pg_policies 
        where tablename = 'requests' and policyname = 'anyone_can_submit_request'
    ) then
        create policy "anyone_can_submit_request"
            on requests for insert
            to anon
            with check (true);
    end if;
end $$;
