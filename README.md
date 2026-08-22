# 我的长理后端

这是体育预约、独立失物招领、资料库及管理后台后端。`server.py` 继续服务本机/USB 开发；Production 使用 Nginx、Waitress、显式安全配置和独立持久目录。

## 特性

- Python 3.11+；Production App Server 为 Waitress，支持范围见 `requirements.txt`，正式部署使用精确版本的 `requirements.lock.txt`。
- SQLite WAL 数据库与外键约束。
- 密码使用 `scrypt` 加盐散列；会话令牌仅以 SHA-256 摘要保存。
- 体育预约通过数据库事务和“有效预约唯一索引”防止重复占用。
- 失物招领支持列表、搜索、发布、编辑、删除、收藏、认领申请和状态管理。
- 旧 Android 本地帖子可使用 `clientLocalId` 幂等导入，防止重复迁移。
- 手机号独立账号用不可逆 HMAC 索引做精确匹配，列表默认只显示脱敏值；完整号码以独立本地密钥加密，只有具备权限的管理员说明原因后才能短暂查看。
- 体育预约支持用户自主填写并明确同意保存“预约识别学号”，它不调用学校系统，也不是校方认证；预约会保存必要的资料快照。
- 资料库支持管理员发布外部链接、学生收藏/最近浏览/投诉，以及后台下架和审计；普通用户不能上传资料。
- 管理员接口支持用户、预约规则、场馆库存、失物、资料、公告、反馈和审计记录。
- 公共启动接口提供公告、Android 版本和远程功能开关。
- Production 配置缺失/占位会 fail fast；提供在线一致性备份、校验和离线恢复工具。
- 图片在鉴权后由 Pillow 完整解码、限制像素并重新编码，不接受客户端文件路径。

## 本地启动

在本目录执行：

```powershell
python -m pip install -r .\requirements.txt
python .\server.py
```

默认监听 `127.0.0.1:8787`，数据库位于 `data\my-changli.db`。Android USB 调试使用：

```powershell
adb reverse tcp:8787 tcp:8787
```

Debug App 默认访问 `http://127.0.0.1:8787/api/v1/`。也可通过 Android 工程根目录、不纳入 Git 的 `local.properties` 设置：

```properties
campusTools.apiBaseUrl.debug=http://127.0.0.1:8787/api/v1/
```

## 工程结构

- `server.py`：路由与业务实现（认证、体育预约、失物招领、资料库、反馈、运营管理、功能开关、版本管理）。
- `core.py`：无路由依赖的基础能力，包括错误模型 `ApiError`、时间戳、SQLite 连接与事务、参数校验、统一分页、JSON 响应序列化，以及测试数据库隔离入口 `configure_test_runtime()`。
- `schema.sql`：初始表结构与索引；增量列通过 `initialize_database()` 幂等补齐。
- `admin/`：同源管理后台静态页面（`index.html`、`app.js`、`styles.css`）；Production 入口为 `https://api.mychangliapp.cn/admin/`。
- `tests/test_api.py`：端到端 API 测试；每个测试进程使用独立临时数据库，绝不触碰本地运营库。
- `manage.py`：数据库维护辅助脚本；`start-admin.ps1`：一键启动本地后端、后台与 ADB 端口映射检查。

## 测试

```powershell
python -m unittest discover -s .\tests -v
```

测试会使用独立临时数据库。测试模块通过 `core.configure_test_runtime()` 显式切换数据库路径，并拒绝把测试指向本地开发库；因此自动化测试不会污染 `data\my-changli.db`。测试产生的账号与业务数据均以自动化前缀/随机值标记，并在临时目录销毁时自动清理。

## 身份与管理员

App 使用手机号注册/登录“我的长理”独立账号；客户端会话令牌由 Android Keystore 加密保存。旧版本的随机安装标识只用于一次性归并本机已有预约、失物与收藏数据，不是正式登录方式。

教务查询与我的长理账号严格分离。教务账号、密码、Cookie、验证码、课表、成绩和考试信息不得进入本后端。体育预约中的“预约识别学号”只来自用户自主填写和明确同意，不读取教务系统。

如需本机管理接口测试，只在当前终端设置 `MY_CHANGLI_ADMIN_USERNAME` 和 `MY_CHANGLI_ADMIN_PASSWORD` 后启动服务。密码至少 12 位。不要把真实值写入源码、文档或 Git。

## Production

完整入口：

- `docs/production/BACKEND_ARCHITECTURE.md`
- `docs/production/DEPLOYMENT_SOP.md`
- `docs/production/ENVIRONMENT_VARIABLES.md`
- `docs/production/DATABASE_BOOTSTRAP.md`
- `docs/production/DATABASE_BACKUP.md`
- `docs/production/DATABASE_RESTORE.md`
- `docs/contracts/ANDROID_BACKEND_CONTRACT.md`

正式依赖使用 `python -m pip install -r requirements.lock.txt` 安装；启动命令为 `python production.py`，且必须由 systemd 使用仓库外 EnvironmentFile。`deploy/` 提供 Nginx 和 systemd 占位模板。

## 当前边界

- 模拟支付仍只表示流程确认，不连接任何支付平台。
- 没有预约码、扫码核验、退款或校方真实场馆规则。
- V1 已有轻量账号/IP 限流，但尚未接入短信验证；是否需要短信必须在账号风险与成本评审后决定。
- V1 Production 候选确定为单机 SQLite WAL；出现持续锁竞争、多实例或水平扩展需求时再迁移 PostgreSQL。
- 真实域名、服务器、备案、DNS、TLS、正式 secrets、异地备份与告警渠道仍属于未来云部署步骤。
