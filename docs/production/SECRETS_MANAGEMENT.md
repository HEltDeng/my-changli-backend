# Secrets Management

## 存放边界

- 真实生产环境文件建议为 `/etc/mychangli/backend.env`，owner 为 root、group 为 Backend 服务组、mode `0640`。
- `.env`、管理员密码、HMAC secret、Fernet key、未来数据库凭据和 TLS private key 全部在 Git 仓库外。
- TLS key 由 Nginx/证书工具读取，Backend 服务用户不应获得读取权限。
- 测试只使用进程级 `TEST_ONLY` 随机值，测试结束删除临时目录，不提交环境文件。

## 生成示例

这些命令应在受控部署终端执行，输出直接进入密码管理器或受限 env 文件，不粘贴到 issue、聊天、Git 或普通日志：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

本仓库、本轮测试和交接包没有生成真实 Production secret。

## 轮换

1. 管理员密码：`python manage.py reset-password <username>`，随后注销旧 session；高风险事件后立即轮换。
2. PII key：维护模式、备份、逐条解密并用新 key 重加密、验证、再切换；旧 key 保留到回滚窗口结束。
3. Phone hash secret：维护模式、备份、用可解密手机号/资料重新计算全部摘要与唯一性校验后切换；直接替换会导致登录查找失败。
4. TLS key：由 Nginx 证书流程轮换并 reload，不进入 Backend env。

泄露时先下线受影响入口、吊销/轮换、审查最小日志和管理员审计，再恢复。绝不通过“启动时随机生成新长期 key”掩盖缺失；Production 配置会 fail fast。
