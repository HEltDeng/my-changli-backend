# 环境配置（Development）

Production 变量、失败行为和轮换要求以 `docs/production/ENVIRONMENT_VARIABLES.md` 为准；本文件只描述本地开发。

- Python：3.11 或更高版本；首次运行执行 `python -m pip install -r requirements.txt`。
- 默认地址：`127.0.0.1:8787`。
- 默认数据库：`data\my-changli.db`。
- 上传图片：`uploads\`。
- 本地管理员：使用 `python .\manage.py create-admin <用户名>` 创建，密码在隐藏输入中填写。

程序不会自动读取 `.env` 文件。`.env.example` 现在展示 Production 占位结构，不能原样用于启动；真实密码和密钥只应存在于当前终端、系统安全存储或不纳入 Git 的本地配置中。

手机号精确搜索使用独立 HMAC 密钥；受控完整号码查看使用独立 Fernet 加密密钥。两者缺省时会在已忽略的 `data\` 目录生成本机私有密钥。备份数据库时必须同时备份这两个密钥，否则完整手机号将无法恢复；不要把它们放入 Git 或验收日志。

Android Debug 默认通过 `http://127.0.0.1:8787/api/v1/` 访问；USB 设备需要 ADB 反向端口映射。Production 必须使用 `production.py`，不能运行开发 HTTP server。
