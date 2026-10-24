# 宣传助手 · Streamlit 云端版

Python 3.12 或 3.13。仓库根目录包含 app.py、requirements.txt、pages 和 prompts。

## 部署

1. 将本文件夹内容上传到新的 GitHub 仓库，确保 .gitignore 也已上传。
2. 打开 https://share.streamlit.io/ ，创建应用，选择仓库、分支和 app.py。
3. 在 Advanced settings 选择 Python 3.12，部署。
4. 访问者在侧栏填写自己的 DeepSeek / 火山方舟 API 密钥、已开通模型 ID，核对单价和预算后使用。

此发布版采用访问者自带密钥方式，不读取部署者的 DEEPSEEK_API_KEY 或 ARK_API_KEY，也无需在 Secrets 放置你的收费密钥。默认模型仅为可编辑初始值，须以访问者账号已开通的模型为准。

## 会话隔离

- 密钥仅存于 Streamlit session_state，没有“保存到本机”按钮；保存配置的底层函数也禁止写入。
- 每个浏览器会话使用操作系统生成的独立临时目录，预算、文案资料、提示词版本、API 日志、生图结果和人工确认历史均在该目录内。
- Python 模块会跨会话共用，因此路径在每次操作时解析，不在导入时缓存。历史页和旧图片日志导入也限制在本会话内。
- 不提供登录或跨设备历史。刷新页面、新建标签页、连接失效或服务重启可能创建新会话。请下载图片、JSON、提示词和预算账本保存；新会话预算不能代表 API 账户的累计费用。
- 临时目录由会话持有的 TemporaryDirectory 在对象回收时清理；不会承诺断线后立即删除。
- 密钥需要经过应用服务器转发给对应 API 服务商；会话隔离不等于对服务器管理员加密。

## 发布内容与检查

只从源码、默认提示词和页面白名单生成发布包，未包含原用户的密钥、预算、运行日志、个人提示词修改或历史数据。

.gitignore 不能撤回此前已提交的密钥；如使用旧仓库，先检查跟踪文件和提交历史。密钥已泄露时应撤销并更换。

本地验证（不产生 API 费用）：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
streamlit run app.py
```

此目录是独立云端发布版，不覆盖桌面便携版。云端部署入口为 app.py；Streamlit Cloud 部署及真实 API 调用仍需单独验收。

## 本轮验证

5 项离线检查通过，覆盖两个会话隔离、密钥不落盘、无会话禁止存取、三个页面渲染及源码配置检查。本机 AppTest 的未运行事件循环使用测试替身，以适应受限 Windows 环境；未验证真实网络服务启动、云端部署或付费 API 调用。

目标仓库：https://github.com/REXReverie-zero/shop-audit
部署入口：app.py。
