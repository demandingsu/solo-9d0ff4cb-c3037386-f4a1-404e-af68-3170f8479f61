# 访谈语料训练分发平台

面向访谈资料员的本地网页应用（FastAPI + SQLite + Docker Compose）。资料员上传已获授权的访谈文本，
按批次在后台生成训练用 CSV，查看状态并下载；当受访者撤回授权时，其语料立即停止分发。

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>。

数据（SQLite 与导出文件）保存在命名卷 `portal-data`，`docker compose restart` / `up` 会复用，
因此撤回状态、待清理记录都会跨重启生效。要彻底重来：`docker compose down -v`。

本地直接运行（需要 Python 3.11+）：

```bash
pip install -r requirements.txt
uvicorn training_portal.main:app --port 8000
```

## 页面能做的事

1. **上传 CSV**：包含 `batch`（访谈批次）、`respondent_id`（受访者编号）、`text`（文本）三列；
   也接受中文表头「访谈批次 / 受访者编号 / 文本」。编码支持 UTF-8 与 GB18030。
2. **生成训练 CSV**：从下拉框选择批次，后台线程按 20 行一个分块写出临时文件，完成后发布为可下载文件。
   **任务提交的同一事务会把该批次当时的语料行快照进数据库（`job_utterances`），本次生成的语料范围就此固定**：
   提交后才上传的补充访谈不属于该快照，不会混入正在排队或生成中的任务，只会进入下一次生成；
   快照持久在 SQLite 中，任务排队期间、服务重启后范围都不变，进度分母恒为提交时的行数，与实际导出行数一致。
3. **查看状态**：任务（排队 / 生成中 / 演示暂停 / 已完成 / 已阻止 / 失败）与导出文件
   （可下载 / 待清理 / 已删除）都有实时状态，页面会就地刷新行内容而不整页重载。
4. **撤回授权**：输入受访者编号，立即删除其全部语料行、锁定并清理所有含其文本的导出文件。
5. **下载**：仅 `ready` 状态可下载，且每次下载都现场复核授权。

`/api/status` 提供同样的状态 JSON，便于脚本化演示。

## 撤回的关键语义

| 场景 | 行为 |
| --- | --- |
| 任务提交后又上传同批次补充行 | 新行不在提交时的语料快照内，不进入本次生成（进度与导出都保持提交时行数），只进入下一次任务 |
| 任务排队期间重启服务 | 语料快照在 SQLite 中持久保留，重启后任务按原范围继续，补充行仍被排除 |
| 撤回已生成的导出文件 | 立即置为 `pending_delete`，后台删除；删除前不可下载 |
| 撤回时该批次正在生成 | 下一个分块检测到撤回即中止整个任务，结果不发布（其文本与其他语料交错，只删其行仍会泄漏） |
| 撤回时任务仍在排队 | 撤回事务经任务快照匹配到该受访者，任务立即标记 `blocked`，固定语料不会延迟撤回 |
| 撤回时任务停在「演示暂停」 | 任务被标记 `blocked`，发布闸门拒绝发布，临时文件删除 |
| 撤回后重新上传同号 | 上传时按编号过滤，计入「跳过撤回」，不会重新纳入 |
| 撤回后原文换了编号再传 | 撤回时记录文本 SHA-256，同文换号同样被拒 |
| 下载时发现已撤回 | 拒绝下载（HTTP 410），文件转入 `pending_delete` 并清理 |
| 删除失败 | 记录保持 `pending_delete`（含尝试次数与错误），永不可下载；后台与启动时持续重试 |
| 崩溃/重启 | 启动时删除残留临时文件与无主导出文件；中断的 `running`/`paused` 任务标记失败；`pending_delete` 继续清理 |

撤回记录、语料删除、导出锁定与任务中止在**同一个 SQLite 写事务**中提交，所有读取路径
（上传、生成、发布、下载）都在事务内读撤回表，因此不存在「已撤回但仍可导出/下载」的中间态。

## 演示开关

页面「演示设置」面板，或环境变量（首次启动写入 SQLite）：

| 开关 | 环境变量 | 默认 | 作用 |
| --- | --- | --- | --- |
| 生成暂停 | `PORTAL_PAUSE_GENERATION=1` | 关 | 文件写好但停在发布前（状态「演示暂停」），在这段窗口里撤回即可看到任务被阻止、文件不发布。再次关闭开关或最多 5 分钟后继续。 |
| 删除失败次数 | `PORTAL_DELETE_FAILURES=3` | 0 | 接下来 N 次删除尝试模拟失败，文件停留「待清理」；耗尽后自动删除成功，可演示「待清理 → 清理完成」。 |
| 删除持续失败 | `PORTAL_DELETE_ALWAYS_FAIL=1` | 关 | 删除一直失败，用于演示重启后继续清理且文件始终不可下载。 |

开关保存在 SQLite 中，重启后仍生效——这正是「重启后继续清理、不重新开放残留文件」的演示条件。
页面上的「立即重试清理」会清空失败次数预算并立即重试。

## 推荐的演示流程

```
docker compose up --build            # 打开 http://localhost:8000
1. 载入样例 batch_2026_spring.csv
2. 选择 2026-春季-01，后台生成 → 下载
3. 撤回 R-1001 → 该导出立即变为不可下载并转为「已删除」
4. 载入样例 reupload_revoked.csv → R-1001 与其原文（改用 R-9001）都被跳过
5. 勾选「生成暂停」→ 生成 2026-春季-02 → 状态「演示暂停」时撤回 R-2001 → 任务「已阻止」，无导出
6. 「删除失败次数」填 3 → 撤回导出中的任一编号 → 导出「待清理」→ 重启容器 → 仍不可下载，预算耗尽后自动删除
```

### 上传交错复现（21 行任务不再写出 22 行）

自动化、可稳定复现的交错验证在测试套件中（`_ChunkBarrier` 让生成器在首个 20 行分块处确定地暂停，
不依赖 sleep）：

```bash
pytest tests/test_portal.py -k "scope or interleaved or progress"
```

- `test_upload_during_generation_keeps_submitted_scope_and_progress`：21 行任务生成到一半时上传补充行，
  任务进度与导出文件保持 21 行，补充行只出现在下一次生成（22 行）。
- `test_queued_job_scope_survives_restart_and_excludes_later_upload`：排队期间上传并重启，范围仍固定。
- `test_revocation_while_queued_blocks_job_through_persisted_scope`：排队中撤回立即阻止，且重启后保持。
- `test_web_progress_matches_export_rows_with_interleaved_upload`：走浏览器同款 HTTP 接口，
  校验 `/api/status` 进度与实际下载文件行数完全一致。

样例：`samples/batch_2026_spring.csv`（两个批次）、`samples/batch_2026_summer.csv`、
`samples/reupload_revoked.csv`（撤回后重传同号 + 原文换号）。

## 代码结构

```
training_portal/
  main.py       FastAPI 路由、页面渲染、下载流
  service.py    CSV 解析、生成/撤回/发布/下载/清理的全部不变量
  db.py         SQLite schema（WAL + BEGIN IMMEDIATE 单写者）
  templates/    单一仪表盘页面
  static/       样式与就地刷新的轮询脚本
tests/          25 个测试，覆盖上传、生成、上传交错下的语料范围固定、排队/重启后范围保留、
                各类撤回时序（含排队中撤回与范围外撤回）、删除失败、重启续清与文本换号
samples/        可直接上传的本地样例
```
