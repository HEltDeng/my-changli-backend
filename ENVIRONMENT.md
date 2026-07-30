# 环境配置

- Python：3.11 或更高版本。
- 默认地址：`127.0.0.1:8787`。
- 默认数据库：`data\my-changli.db`。
- 上传图片：`uploads\`。
- 本地管理员：使用 `python .\manage.py create-admin <用户名>` 创建，密码在隐藏输入中填写。

环境变量可参考 `.env.example`，但程序不会自动读取 `.env` 文件。真实密码和密钥只应存在于当前终端、系统安全存储或不纳入 Git 的本地配置中。

Android Debug 默认通过 `http://127.0.0.1:8787/api/v1/` 访问；USB 设备需要 ADB 反向端口映射。公网阶段必须改用 HTTPS 和独立生产数据库。
