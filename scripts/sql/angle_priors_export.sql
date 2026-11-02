-- angle cards v2: the two read-only exports behind `scripts/build_angle_priors.py --from-export cells.json mix.json`
-- (only for building the vendored snapshot without service credentials; production rebuilds it live).
-- Both return aggregates only: no note ids, no titles, no text. Save each result's JSON rows to a file; do not
-- commit the exports.
--
-- 1) cells.json — label cells per TV project: "bucket,format,lever,question,y,n,rank_sum,train_rank_sum" joined by ";".
--    Same label as deskcore.priors.label: published >= 3 days ago, not TGV_phase1, not synthetic / paid /
--    铺评工单; projects with comments: comments not NULL, y = 爆/大爆 or comments >= 20; projects without: tier
--    爆/大爆/趴 only. bucket = Beijing day since the project's first post: 0 (<10), 1 (10-13), 2 (>=14).
--    Indices follow deskcore/vendor/controlled_vocab_v0_2.json (-1 = missing); question 1 = 是, 0 = 否.
--    rank_sum ranks comments within (project, format) over all eligible notes (the snapshot and the global / brand
--    layers use it); train_rank_sum ranks bucket-0 notes only among the project's own first 10 days (0 elsewhere):
--    the backtest builds the target project's training label from it, so test-period comments never reach a
--    training cell (deskcore.priors_backtest.cells_from_notes does the same). Both rank by format index fi, so a
--    missing and an off-vocabulary format share one group, as in deskcore.priors (format "" there).
with f as (
 select distinct on (subject_id) subject_id note_id, answer from truth_vault.note_feature_answers
 where subject_type='note' and question_id='title_is_question' and run_tag='primary' and coalesce(answer,'')<>''
 order by subject_id, extracted_at desc),
pc as (select project_id, count(comments_count) filter (where publish_time <= now()) > 0 has_cc from truth_vault.notes group by 1),
s as (select project_id, min((publish_time + interval '8 hours')::date) d0 from truth_vault.notes where publish_time <= now() group by 1),
e as (
 select n.project_id, pc.has_cc, ((n.publish_time + interval '8 hours')::date - s.d0) dd,
  coalesce(array_position(array['情感叙事','认知重构','横评对比','教程攻略','直给推荐','场景植入','提问求助','反差破圈']::text[], n.content_format)-1, -1) fi,
  coalesce(array_position(array['焦虑撬动','羞耻撬动','恐惧撬动','愤怒撬动','罪恶感撬动','造梦投射','认同感建立','归属感建立','共鸣释放','虚荣撬动','好奇驱动','信息差利用']::text[], n.emotional_lever)-1, -1) li,
  case f.answer when '是' then 1 when '否' then 0 else -1 end qi,
  -- tier NULL 时 `tier in (...)` 是 NULL, `NULL or false` 还是 NULL: 不 coalesce 的话 y 是 NULL, concat_ws 丢掉那一段,
  -- 格子少一段(review on aw#140)。和 priors.label 一样, 能进来的每一篇都记明确的 0 / 1
  coalesce(coalesce(n.tier in ('爆','大爆'), false) or (pc.has_cc and n.comments_count >= 20), false)::int y,
  n.comments_count cc
 from truth_vault.notes n join pc using(project_id) join s using(project_id) left join f using(note_id)
 where n.publish_time is not null and n.publish_time <= now() - interval '3 days'
   and n.project_id <> 'TGV_phase1'
   and not coalesce((n.data_quality_flags->>'synthetic')::boolean,false)
   and not coalesce((n.data_quality_flags->>'paid_promoted')::boolean,false)
   and not coalesce(n.data_quality_flags->'comment_maintained_routes' ? '铺评工单', false)
   and ((pc.has_cc and n.comments_count is not null) or (not pc.has_cc and n.tier in ('爆','大爆','趴')))),
r as (select e.*, case when has_cc then
   ((rank() over (partition by project_id, fi order by cc) - 1)
    + 0.5 * count(*) over (partition by project_id, fi, cc)) / count(*) over (partition by project_id, fi)
   end rk,
   case when has_cc and dd < 10 then
   ((rank() over (partition by project_id, fi, dd < 10 order by cc) - 1)
    + 0.5 * count(*) over (partition by project_id, fi, dd < 10, cc))
    / count(*) over (partition by project_id, fi, dd < 10)
   end rkt from e),
c as (select project_id, case when dd < 10 then 0 when dd < 14 then 1 else 2 end dd, fi, li, qi, y, count(*) n,
   round(coalesce(sum(rk),0)::numeric, 2) rs, round(coalesce(sum(rkt),0)::numeric, 2) rts from r group by 1,2,3,4,5,6)
select project_id p, string_agg(concat_ws(',', dd, fi, li, qi, y, n, rs, rts), ';' order by dd, fi, li, qi, y) c
from c group by 1 order by 1;

-- 2) mix.json — posting mix per TV project (synthetic notes excluded): window A = the project's own last 180
--    days of data, window B = the last 180 days from now. "<window><dim><index>:<count>" joined by ";", dims
--    f/l/a/i/t = format / lever / archetype (1/len each) / intensity / trends (1/len each), q = title answered
--    Q / N, n = posts. Scheduled notes (publish_time in the future) are not published yet and count nowhere,
--    here, in the span or in the first-post day above (same as deskcore.priors.published_at).
with f as (
 select distinct on (subject_id) subject_id note_id, answer from truth_vault.note_feature_answers
 where subject_type='note' and question_id='title_is_question' and run_tag='primary' and coalesce(answer,'')<>''
 order by subject_id, extracted_at desc),
b as (select n.*, f.answer tq, max(n.publish_time) over (partition by n.project_id) p_last
      from truth_vault.notes n left join f using(note_id)
      where n.publish_time <= now() and not coalesce((n.data_quality_flags->>'synthetic')::boolean,false)),
w as (select 'A' win, b.* from b where publish_time >= p_last - interval '180 days'
      union all select 'B', b.* from b where publish_time >= now() - interval '180 days'),
u as (
 select project_id, win, 'f' d, (array_position(array['情感叙事','认知重构','横评对比','教程攻略','直给推荐','场景植入','提问求助','反差破圈']::text[], content_format)-1)::text v, 1.0 x from w where content_format is not null
 union all select project_id, win, 'l', (array_position(array['焦虑撬动','羞耻撬动','恐惧撬动','愤怒撬动','罪恶感撬动','造梦投射','认同感建立','归属感建立','共鸣释放','虚荣撬动','好奇驱动','信息差利用']::text[], emotional_lever)-1)::text, 1.0 from w where emotional_lever is not null
 union all select project_id, win, 'a', (array_position(array['同辈比较','伴侣关系','代际冲突','职场关系','宠物相关','自我形象维护','身份认同','时间流逝感','自由意志','阶层焦虑','经济焦虑','健康焦虑','育儿焦虑','情感缺位','归属缺失','认同缺失','控制感渴望','自我提升','消费愉悦']::text[], a)-1)::text, 1.0/cardinality(human_truth_archetype) from w, unnest(human_truth_archetype) a where cardinality(human_truth_archetype) > 0
 union all select project_id, win, 'i', (array_position(array['low','medium','high']::text[], emotional_intensity)-1)::text, 1.0 from w where emotional_intensity is not null
 union all select project_id, win, 't', (array_position(array['特定平台事件','特定IP引用','时事热点','季节性事件','节日','行业事件','当代流行词','时代语言范式','平台话术','通用']::text[], t)-1)::text, 1.0/cardinality(trend_dependencies) from w, unnest(trend_dependencies) t where cardinality(trend_dependencies) > 0
 union all select project_id, win, 'q', case tq when '是' then 'Q' else 'N' end, 1.0 from w where tq is not null
 union all select project_id, win, 'n', '', 1.0 from w),
g as (select project_id, win, d, v, round(sum(x)::numeric, 3) s from u group by 1,2,3,4)
select g.project_id p, p.category c, (select min(publish_time)::date||'/'||max(publish_time)::date from truth_vault.notes x where x.project_id=g.project_id and x.publish_time <= now()) span, now()::text built,
 string_agg(win||d||v||':'||s, ';' order by win, d, v) m
from g join truth_vault.projects p using(project_id) group by 1,2 order by 1;
