# JobPicky 开发须知

本文只记录当前仓库仍然有效的开发约束、容易踩坑的地方和验收命令。产品使用说明以 `README.md` 为准。

## 当前结构

- `src/jobpicky/`：应用源码；使用 `src` layout。
- `tests/`：pytest 测试。
- `scripts/release_check.py`：发布前的干净安装验收入口。
- `pyproject.toml`：依赖、Python 版本、构建和 pytest 配置。
- `src/jobpicky/resources/`：必须随 wheel 发布的资源，例如 seed SQLite 数据库和 WebUI 模板。

项目要求 Python `>=3.11`。应用是本地 WebUI，用户数据默认位于 Windows 的 `%LOCALAPPDATA%\JobPicky\`；测试可以使用 `JOBPICKY_HOME` 指向临时目录。

## 源码安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

日常开发直接使用该虚拟环境运行测试；普通用户不需要执行这套源码安装流程。

### CLI 维护命令

CLI 主要面向开发、自动化和本地数据维护，普通用户优先使用 WebUI：

```powershell
# 每日增量扫描、匹配、可选飞书同步和提醒
jobpicky daily

# 只更新本地，不调用飞书
jobpicky daily --no-feishu

# 按当前偏好重新匹配历史岗位
jobpicky rematch --no-feishu

# 导出岗位或推荐到 Excel
jobpicky export --table recommended --output data/exports/recommended.xlsx

# 从飞书回收求职状态、下次行动和备注
jobpicky pull
```

CLI 默认读取当前目录下的 `config.yaml` 和 `data/`。发布前的构建、干净安装和版本验收流程见本文后面的“发布验收”部分。

## 固定的 UI 沙盒测试流程

需要在浏览器中检查 UI、首次安装、扫描过程或多次使用后的状态时，统一使用
`scripts/ui_sandbox.py`。默认沙盒位于 `.test-results/ui-sandbox/`，已经被 Git
忽略，其中的虚拟环境、配置、SQLite、日志和导出内容都不会进入用户的
`%LOCALAPPDATA%\JobPicky\`，也不会写入仓库源码目录。

### 从头部署并测试

```powershell
python scripts/ui_sandbox.py fresh
```

这条命令固定执行以下流程：

1. 只删除 `.test-results/ui-sandbox/` 中的旧测试环境；
2. 创建全新的隔离虚拟环境；
3. 从当前工作区源码构建并安装一份非 editable 的项目副本；
4. 使用空白的独立 profile 启动 WebUI，并自动打开浏览器。

因此，每次修改 UI 后需要验证“新用户第一次安装”时，使用 `fresh`。页面中的
引导、配置保存和首次扫描都是真实流程；停止服务后，这次产生的数据会保留。
安装的是命令执行时的代码快照，后续源文件修改不会悄悄改变现有沙盒。

### 保留上次状态继续测试

```powershell
python scripts/ui_sandbox.py continue
```

`continue` 不重新安装、不清空配置、不重置数据库，适合验证：

- 第二次及后续启动；
- 多次扫描后的岗位数量、分页和推荐变化；
- 浏览器刷新、任务恢复和偏好持久化；
- 用户已经完成引导后的日常界面；
- 失败或取消扫描后，上一次已发布结果是否仍然可用。

如果源码已经再次修改，要先执行 `fresh` 才能测试新代码；`continue` 的目的就是
稳定复现上一次安装和数据状态。

服务运行时按 `Ctrl+C` 停止。默认从 `8877` 开始寻找空闲端口；终端会打印实际
地址。自动打开浏览器不方便时使用：

```powershell
python scripts/ui_sandbox.py fresh --no-browser
python scripts/ui_sandbox.py continue --no-browser --port 9000
```

### 查看和清理沙盒

```powershell
python scripts/ui_sandbox.py status
python scripts/ui_sandbox.py clean
```

`status` 只读取沙盒状态，显示安装、配置和数据库是否存在。`clean` 只允许删除
仓库内部明确指定的沙盒目录，并带有路径保护，不会删除仓库根目录。通常无需手动
清理，因为下一次 `fresh` 会自动重建。

### 建议的常见验收顺序

一次 UI 改动建议按以下顺序检查：

1. 运行 `fresh`，检查首次引导、空状态、偏好保存和首次扫描；
2. 停止后运行 `continue`，检查已有数据、分页、筛选和再次扫描；
3. 在扫描中刷新页面、取消任务，确认进度能恢复且旧结果不变；
4. 再次运行 `continue`，确认配置和数据库跨进程保留；
5. 最后运行自动化测试；需要模拟正式发行包时，仍使用下文的
   `scripts/release_check.py`。

UI 沙盒用于人工交互回归，不替代 pytest 或发布验收。不要把真实飞书密钥填入
沙盒；如确实需要测试集成，只使用专门的测试应用和测试 Base。

## 必须注意的坑

### 中文和文件编码

- 源码、YAML、Markdown、HTML 和测试文件统一按 UTF-8 处理。读写文件时显式指定 `encoding="utf-8"`，不要依赖 Windows PowerShell 或系统默认编码。
- 终端显示乱码不等于文件已经损坏。先用 UTF-8 编辑器或 Python 读取原始内容确认，不要因为终端显示异常就批量转码无关文件。
- 不要在无关改动中修改现有中文文案、编码形式或测试中的 Unicode 样例；编码修复必须有明确范围并配套测试。
- YAML 写回时保持 Unicode 内容可读，沿用 `config.py` 的 UTF-8 和 `allow_unicode=True` 约定。
- 需要验证中文、YAML 或 HTML 时，检查文件实际字节和解析结果，不要只根据控制台渲染结果判断。

### 配置、数据和密钥

- 不提交真实的 Feishu Base URL、App Secret、运行中的 SQLite、用户配置、日志、导出文件或备份。
- `config.yaml`、`data/`、`dist/`、`build/`、虚拟环境和 `.test-results/` 都是本地或构建产物；确认 `.gitignore` 后再提交。
- `jobs_seed.sqlite` 是仓库内的只读发布资源，不要用用户运行数据覆盖它。
- 测试必须使用临时目录、fake client 或 mock，不访问真实 Feishu 数据，不把凭据写入断言、异常消息或日志。

### 路径和进程

- 不要假设当前工作目录是仓库根目录；应用路径通过 `AppPaths` 管理，测试用 `tmp_path` 或 `JOBPICKY_HOME` 隔离。
- WebUI 测试必须禁用浏览器自动打开，并在临时端口、临时数据目录中启动服务。
- 修改 launcher、资源打包、依赖或 `pyproject.toml` 后，必须做一次干净 wheel 安装验收；仅通过源码路径运行不能证明安装包正确。
- `release_check.py` 会清理并重新生成 `dist/`，不要在脚本运行期间依赖旧构建产物。

## 测试和验收

### 一次性开发环境准备

```powershell
python -m pip install -e ".[test]"
```

如果只需要运行已有测试，环境准备好后不需要每次重新创建虚拟环境或重新安装项目。

### 日常开发：普通测试

```powershell
python -m pytest -q
```

日常代码修改至少执行这条命令。提交前要求全部测试通过；新增或修改行为时，必须同步维护对应测试。普通测试验证代码逻辑，不验证 wheel 内容、安装依赖或用户机器上的启动路径。

### 发布验收：干净安装测试

```powershell
python scripts/release_check.py
```

以下情况必须执行：

- 修改依赖、`pyproject.toml`、构建配置或资源打包；
- 修改 launcher、WebUI 启动路径或本地数据目录；
- 准备合并、打 tag 或发布 PyPI；
- 修改可能影响安装后导入、静态资源或依赖解析的代码。

脚本会自动构建 wheel/sdist、验证 wheel 内容、创建全新临时虚拟环境、安装 wheel、执行 `pip check`，再从仓库外启动 WebUI，检查首页、`/api/health`、运行目录创建和干净退出。

脚本还会使用 `uvx --from <wheel> --python 3.12` 从仓库外启动一次发行 wheel，验证目标用户入口和 Python 3.12 解析路径。

成功输出应为：

```text
[PASS] Build package
[PASS] Verify wheel
[PASS] Create clean environment
[PASS] Install package
[PASS] Dependency check
[PASS] Launch outside repository
[PASS] WebUI health check
[PASS] uvx launch smoke
[PASS] Clean shutdown

RESULT: PASS  9/9
```

失败时终端只显示失败阶段、简短原因和日志位置；完整构建、pip 和服务日志位于 `.test-results/release-check.log`。不要把完整日志复制进提交说明或测试断言。

## 提交前检查

```powershell
python -m pytest -q
git diff --check
git status --short
```

如果触发了发布验收条件，再执行：

```powershell
python scripts/release_check.py
```

发布 PyPI 时更新 `pyproject.toml`、`src/jobpicky/__init__.py` 和 tag 的版本号，然后推送形如 `v0.2.0` 的 tag。GitHub Actions 会先运行测试和 Windows 发布验收，再使用 PyPI Trusted Publishing 上传 wheel 和 sdist。

提交前确认没有凭据、运行数据、临时目录或无关格式化变更。GitHub Actions 的普通测试和发布验收应复用上述入口；不要重新在 workflow 中堆叠独立的 wheel 构建、venv 安装、`pip check` 或 smoke 命令。
## GitHub Release

1. `git fetch origin`，将功能分支合入 `main`。
2. 运行 `python -m pytest -q` 和 `python scripts/release_check.py`。
3. 依次推送 `main`、带注释的版本 tag：

   ```powershell
   git push origin main
   git tag -a vX.Y.Z -m "JobPicky vX.Y.Z"
   git push origin vX.Y.Z
   ```

4. tag 不会自动生成 GitHub Release。用 GitHub REST API `POST /repos/{owner}/{repo}/releases` 创建 Release，正文读取对应的 `docs/release-notes/vX.Y.Z.md`。
5. 用 `git ls-remote origin` 和 Release API 回读确认远端状态。大对象推送超时时先运行 `git gc` 再重试。
