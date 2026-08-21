# Codex Quota Compass

一个只读的 Codex Skill，用来查看当前订阅的周用量窗口，并根据已消耗 Credits 和已用百分比粗略反推整周额度。

## 能查到什么

- 已用和剩余百分比、周窗口和重置时间
- 当前周窗口的 Credits、Token、turns 和 threads
- 最近两个七日自然窗口的对比
- 数据完整时，粗略反推的周总 Credits 和剩余 Credits
- Credits 来源、计价覆盖率和无法识别的模型

## 安装

macOS 或 Linux：

```bash
mkdir -p ~/.codex/skills
git clone https://github.com/147228/codex-quota-compass.git ~/.codex/skills/codex-quota-compass
```

Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.codex\skills" | Out-Null
git clone https://github.com/147228/codex-quota-compass.git "$env:USERPROFILE\.codex\skills\codex-quota-compass"
```

也可以下载 GitHub 提供的 ZIP，解压后把文件夹放到 `~/.codex/skills/codex-quota-compass/`。

## 使用

回到 Codex，输入：

```text
查查我的 Codex 20×，有多少 Credits
```

也可以直接运行脚本：

```bash
python3 scripts/check_codex_quota.py
```

机器可读结果：

```bash
python3 scripts/check_codex_quota.py --json
```

## 它是怎么计算的

Skill 会从本机 `~/.codex/auth.json` 读取当前 Codex 登录凭证，然后访问 `https://chatgpt.com` 下的只读用量接口。它先取得周窗口的已用百分比，再汇总同一窗口内的 Credits。

粗略周总额度的公式是：

```text
周总 Credits ≈ 当前周窗口已消耗 Credits ÷ 已用比例
```

已用比例较低时，页面的整数百分比会带来较大误差。建议在已用 20% 和 50% 左右各保存一次结果。

部分账号的每日用量接口不会直接返回 `totals.credits`。Skill 会尝试读取按模型和 Token 类型拆分的明细，再按公开 Rate Card 回算。数据不完整时，它会停止反推，不会把缺失值当成 0。

## 安全边界

- 访问令牌只发往 `https://chatgpt.com`
- 不打印令牌、邮箱、`user_id` 或 `account_id`
- 不修改账号、额度、登录态或客户端指纹
- 后台接口没有稳定的公开承诺，OpenAI 修改接口后本 Skill 也需要更新

## 致谢

自查思路来自 [BlueSkyXN](https://linux.do/t/topic/2137136)，油猴可视化脚本来自 [yfzz／Jun Zhao](https://linux.do/t/topic/2138324)，最近七天窗口的改进来自 [Licoy](https://linux.do/t/topic/2786559)。
