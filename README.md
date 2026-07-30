# 我的长理后端 MVP

这是体育预约、独立失物招领及后续管理后台的本地开发后端。当前仅供本机和 USB 调试设备联调，不应直接暴露到公网。

## 特性

- Python 3.11 标准库运行，无第三方依赖。
- SQLite WAL 数据库与外键约束。
- 密码使用 `scrypt` 加盐散列；会话令牌仅以 SHA-256 摘要保存。
- 体育预约通过数据库事务和“有效预约唯一索引”防止重复占用。
- 失物招领支持列表、搜索、发布、编辑、删除、收藏、认领申请和状态管理。
- 旧 Android 本地帖子可使用 `clientLocalId` 幂等导入，防止重复迁移。
- 管理员接口预留场馆、库存、预约、失物、公告和反馈管理能力。
- 公共启动接口提供公告、Android 版本和远程功能开关。

## 本地启动

在本目录执行：

```powershell
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

## 测试

```powershell
python -m unittest discover -s .\tests -v
```

测试会使用临时数据库，不接触开发数据库。

## 身份与管理员

App MVP 使用随机安装标识换取设备级账号，会话保存在应用私有存储。接口同时支持用户名密码注册与登录，为后续正式账号体系保留迁移路径。

如需本机管理接口测试，只在当前终端设置 `MY_CHANGLI_ADMIN_USERNAME` 和 `MY_CHANGLI_ADMIN_PASSWORD` 后启动服务。密码至少 12 位。不要把真实值写入源码、文档或 Git。

## 当前边界

- 模拟支付仍只表示流程确认，不连接任何支付平台。
- 没有预约码、扫码核验、退款或校方真实场馆规则。
- 设备级账号只适合 MVP；公网前必须接入正式身份认证、验证码/限流、审计日志和密钥托管。
- SQLite 适合本地开发与单机部署；正式多人生产环境建议迁移至 PostgreSQL，并保留同等唯一约束与事务语义。
- 公网部署前还必须配置 HTTPS、反向代理、备份、监控、内容审核和数据合规策略。
