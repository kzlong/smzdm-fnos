# 什么值得买签到 · fnOS 原生 FPK

基于 [enwaiax/smzdm-bot](https://github.com/enwaiax/smzdm-bot)项目，支持多账号、手动和定时签到、Bark 通知、Cookie 显示切换以及签到记录清空的FPK应用。测试版本fnOS 版本为 1.2.1011，依赖 Python 3.12 。

## 配置

Cookie 必须包含：

- `sess`
- `smzdm_id`
- `device_id`

填入格式：

`sess=xxxxxxx;smzdm_id=xxxxx;device_id=xxxxxxxx;`

## 界面



## 来源与许可

签到核心来自 [enwaiax/smzdm-bot](https://github.com/enwaiax/smzdm-bot)，固定为 `d495602b872d3b1aecb46cfbd51d3a869cbe30ea`。源码包包含该提交的客户端源码及 Apache-2.0 许可证。fnOS 服务、页面和打包代码采用 Apache-2.0，构建会保留第三方库自带的许可文件。
