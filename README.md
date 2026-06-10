# 群聊日常分析

基于群聊记录生成精美的日常分析报告：话题总结、用户称号 (MBTI)、群圣经金句、聊天质量锐评、活跃度统计与可视化图表。

> 📦 **本插件 1:1 移植自 AstrBot 插件 [astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis)**（作者 [SXP-Simon](https://github.com/SXP-Simon) 等，MIT License）。
> 分析逻辑、提示词与全部 8 套报告模板均来自原仓库，并针对 nekro-agent 的插件体系、模型组与渲染环境做了适配。详细功能说明、模板效果图可参阅原仓库 README。

---

## ✨ 功能

- **📊 统计数据**：消息总数、参与人数、字符数、表情数、24 小时活跃度图表、最活跃时段
- **💬 话题分析**：LLM 提取热门话题与讨论要点，话题详情中的用户自动渲染为头像胶囊
- **🏆 用户称号**：基于发言行为（频率/长度/夜间比例/回复比例等）分配个性化称号 + MBTI
- **📜 群圣经**：自动筛选群里最逆天的金句并附点评
- **🔥 质量锐评**：将聊天内容抽象为多个维度，按占比条形图呈现，配 bot 头像 + 锐评气泡
- **🖼️ 多种输出**：图片（默认）/ 纯文本 / HTML 文件（发送到群文件）
- **🎨 8 套模板**：scrapbook（默认）/ retro_futurism / HatsuneMiku / hack / ATRI / simple / format / spring_festival
- **⏰ 定时分析**：每天在指定时间点自动生成日报（支持多时间点、白/黑名单、多群错峰）
- **🔄 增量分析**：消息量大的群可用滑动窗口模式，分批提取、自动去重、汇总成报告，失败自动回退全量

## 🚀 安装部署

### 1️⃣ 部署 HTML 渲染服务（⚠️ 必须先做这一步）

图片报告依赖一个 **browserless/chromium 无头浏览器容器** 把 HTML 渲染成图片。**不部署它图片格式会一直失败**（自动回退为纯文本报告，但就没有精美报告图了）。

```bash
# 拉取渲染镜像（约 1.5GB，国内网络可能较慢请耐心等待）
docker pull ghcr.io/browserless/chromium:latest

# 启动渲染容器（必须与 nekro_agent 在同一个 docker 网络，容器名保持 nekro_html2img）
docker run -d --name nekro_html2img \
  --restart unless-stopped \
  --network nekro_network \
  --memory 1g \
  -e CONCURRENT=1 \
  -e TIMEOUT=180000 \
  ghcr.io/browserless/chromium:latest
```

> 💡 `--network` 的值要和 nekro_agent 容器一致，可用 `docker inspect nekro_agent --format '{{json .NetworkSettings.Networks}}'` 查看实际网络名（默认部署一般是 `nekro_network`）。
> 网络名/容器名不同的话，请到插件配置中同步修改 `RENDER_ENDPOINT`（默认 `http://nekro_html2img:3000`）。

验证渲染服务是否可用：

```bash
docker exec nekro_agent sh -c "/app/.venv/bin/python -c \"
import httpx
r = httpx.post('http://nekro_html2img:3000/screenshot', json={'html': '<h1>ok</h1>', 'options': {'type': 'png'}}, timeout=60)
print(r.status_code, len(r.content))\""
# 输出 200 和一个字节数即为正常
```

### 2️⃣ 安装插件

把本仓库放进 nekro-agent 的插件目录（宿主机路径以实际部署为准）：

```bash
cd /root/srv/nekro_agent/plugins/packages/
git clone https://github.com/miuzhaii/nekro-plugin-group-analysis group_analysis
docker restart nekro_agent
```

> ⚠️ 目录名必须是 `group_analysis`（与插件 module_name 一致）。
> 重启后查看日志确认加载成功：`docker logs nekro_agent | grep 群聊日常分析`

### 3️⃣ 配置插件

打开 nekro-agent WebUI → 插件管理 → 群聊日常分析：

1. **LLM 模型组**：下拉选择一个聊天模型组（留空用主模型组）
2. **插件管理员**：`MANAGE_SUPER_ADMINS` 填自己的 QQ 号
3. **定时日报**（可选）：`AUTO_ANALYSIS_TIMES` 设时间点，把群号加入「定时分析群列表」
4. **分析人格**（可选）：下拉选择一个人设，报告将以该人设口吻输出

### 4️⃣ 验证

在群里发送 `/群分析`，约 1-3 分钟后收到报告图片。

无依赖安装：插件只使用 nekro-agent 容器内置的库（jinja2 / openai / httpx / pydantic），无需 pip 安装任何额外包。

## 📝 命令

均需 **插件管理员**（配置中的 `MANAGE_SUPER_ADMINS`）或 **群主/管理员**（需开启 `ALLOW_GROUP_ADMIN_MANAGE`）权限：

| 命令 | 说明 |
|------|------|
| `/群分析 [天数]` | 手动分析最近 N 天（默认 1，最大 7），如 `/群分析 3` |
| `/分析设置 [操作]` | `enable` / `disable` / `status` / `test` / `reload` / `incremental_debug` |
| `/设置格式 [名称或序号]` | 切换输出格式 image / text / html |
| `/设置模板 [名称或序号]` | 切换报告模板 |
| `/查看模板` | 查看可用模板列表 |
| `/增量状态` | 查看增量分析滑动窗口状态 |

## ⚙️ 关键配置

- **LLM 模型组**：默认使用 nekro-agent 主模型组，可分析器单独指定（话题/称号/金句/锐评）
- **定时分析**：`AUTO_ANALYSIS_TIMES` 设时间点（如 `23:00`）；`SCHEDULED_GROUP_LIST_MODE` 为 whitelist 时需把群号加入 `SCHEDULED_GROUP_LIST` 才会自动跑
- **增量分析**：把群号加入 `INCREMENTAL_GROUP_LIST`，活跃时段内每隔 `INCREMENTAL_INTERVAL_MINUTES` 分钟自动提取一批，报告时间点汇总
- **分析人格**：`PERSONA_PROMPT` 填入人设后，报告以该人设口吻输出
- **渲染服务**：`RENDER_ENDPOINT` 指向 browserless/chromium 容器（部署见上方「安装部署」第 1 步），渲染失败自动回退文本报告

## 🔧 与原版的差异

- 平台仅支持 QQ (OneBot v11 / NapCat)，不含 Telegram / Discord / 飞书适配
- HTML 报告改为上传到群文件（原版支持自建外链 Web 服务器）
- `/查看模板` 仅列出文字清单，不发预览图
- 图片渲染由 AstrBot T2I 服务改为自托管 browserless 无头浏览器
- 质量锐评气泡旁的角色头像自动使用 bot 的 QQ 头像（原版为模板内置图）
- 人格系统简化为单一 `PERSONA_PROMPT` 配置项

## 🙏 致谢

- 原插件：[SXP-Simon/astrbot_plugin_qq_group_daily_analysis](https://github.com/SXP-Simon/astrbot_plugin_qq_group_daily_analysis) (MIT)
- 灵感来源：[LSTM-Kirigaya/openmcp-tutorial qq-group-summary](https://github.com/LSTM-Kirigaya/openmcp-tutorial/tree/main/qq-group-summary)
