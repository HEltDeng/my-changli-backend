# 我的长理 backend 来源与开发入口

仓库：HEltDeng/my-changli-backend；main 持续向前，不可 reset 回历史提交。组件基线：`30a1c5fb37e7803ecadb2fd43b5b05c34f61eb37`。
本机固定目录：`D:\Codex\01_App-应用开发\001_MyChangLi-我的长理\01_Source-当前源码\02_Backend-Admin`。本机统一导航在项目根 SOURCE_OF_TRUTH.md，本文件仅说明本组件。

便携身份规则与唯一实现位于 Android tools/source-identity.json、Test-MyChangliSourceIdentity.ps1；本机路径在项目外 local-source-entrypoints.json。受控克隆显式使用 Validation，仍检查 origin、产品和基线。
正式 V1.0.5 和生产部署不能被验证产物替换。历史说明不是默认开发入口。

Admin 是本仓 admin 子目录。生产部署 30a1c5f 与开发 main 文档后继区分。测试使用隔离 DB/uploads/logs/backups，不得复制生产用户数据。
