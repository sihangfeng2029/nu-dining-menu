# NU 食堂菜单 · 中英对照

每天自动抓取 Northwestern 五个 dining commons（Allison / Sargent / Plex East / Plex West / Elder）午餐、晚餐的菜名、描述、配料表和过敏标签，翻译成中文，生成一个手机友好的网页。

## 一次性设置（约 3 分钟）

1. 在 GitHub 新建一个 **Public** 仓库（repository），名字例如 `nu-dining-menu`，不要勾选任何初始化选项。
2. 把本压缩包解压后的**全部文件**上传进去（仓库页面 → *Add file* → *Upload files* → 把文件夹里的内容全部拖进去，包括 `.github` 文件夹 → *Commit changes*）。
   - macOS 访达默认隐藏 `.github`，按 `Cmd+Shift+.` 显示隐藏文件后再拖。
3. 仓库 *Settings* → *Pages* → *Build and deployment* → *Source* 选 **GitHub Actions**。
4. 仓库 *Settings* → *Actions* → *General* → *Workflow permissions* 选 **Read and write permissions** → *Save*。
5. 仓库 *Actions* 标签页 → 左侧 *Update menu* → *Run workflow*。约 2–5 分钟后网站上线：
   `https://<你的用户名>.github.io/nu-dining-menu/`
   手机上打开后“添加到主屏幕”，以后像 App 一样点开即用。

之后每天芝加哥时间早上约 4–5 点自动更新一次、上午 10 点左右再刷新一次，无需任何操作。

## 结构

| 路径 | 作用 |
|---|---|
| `scraper/fetch_menu.py` | 调用 DineOnCampus 官方网页背后的 API (apiv4) 抓菜单 |
| `scraper/translate.py` | 英译中：人工词典 → 翻译缓存 → Google 翻译（免费，无需 key） |
| `scraper/glossary.json` | 食材人工词典，**机翻不满意直接改这里**，优先级最高 |
| `scraper/build.py` | 串起抓取 / 翻译 / 生成网站 |
| `web/index.html` | 网页（单文件） |
| `data/menus/` | 每天的翻译后菜单（自动提交，留作历史） |
| `data/translations.json` | 翻译缓存（自动提交，越用越快） |

本地运行：`pip install requests && python scraper/build.py && python -m http.server -d site`
