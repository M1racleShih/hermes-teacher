# Hermes Teacher

[English](README.md)

把一个暂时想不明白的问题，变成下次还能接着上的课。Hermes Teacher 先听你说已经懂了什么，再一起定计划、试着答题。讲解、配图，以及今天还没有解开的问题，都保存在本机的 Markdown 资料库里。

## 先旁听一分钟

> **你** · 我会算两个骰子的点数和，但为什么 7 比 8 常见？
>
> **老师** · 咱俩各拿一颗骰子。你掷出了 1，我要掷几点才能凑成 7？要凑成 8 呢？
>
> **你** · 凑 7 得掷 6。凑 8 就得掷……7。噢，骰子上没有。
>
> **老师** · 对。现在把你手里的点数换成 2，这次能凑出哪些和？

从左边选一颗骰子，沿着那一行看过去。两颗公平骰子一共能掷出 36 种等可能的组合。图里每个位置对应一种，试着数数两条斜线各有多长。

![两颗骰子的点数和。绿色圆圈标出六种凑成 7 的组合，橙色方块标出五种凑成 8 的组合。](assets/two-dice.svg)

上课时可以请老师画图，拿一段读不懂的原文讨论，也可以直接说「不知道」。测验先让你试一试，作答后再显示解析。看完点「继续」，老师才往下讲。

## 开始第一节课

先安装 [Hermes Agent](https://hermes-agent.nousresearch.com/docs/)，让它能使用你选择的模型服务。项目已在 Hermes Agent **v0.23.2** 和 Hermes Desktop **v0.23.3** 上做过课堂验证。把下面的 `YOUR_PROVIDER` 和 `YOUR_MODEL` 换成你在 Hermes 中使用的服务名和模型名。

```sh
git clone https://github.com/M1racleShih/hermes-teacher.git
cd hermes-teacher

bin/hermes-teacher init --vault "$HOME/学习笔记" --language zh \
  --provider YOUR_PROVIDER --model YOUR_MODEL
bin/hermes-teacher chat --topic "为什么 7 更常见"
```

首次初始化会在 `~/.hermes/profiles/teacher` 建立专用配置，目标目录须为空。如果使用已有 Hermes 配置中声明的自定义服务，在 `init` 后加 `--source-profile "$HOME/.hermes"`，即可导入服务地址和凭据环境变量名。启动器找不到 Hermes 时，可把 `HERMES_TEACHER_PYTHON` 设为 Hermes 的 Python 解释器路径。

新配置的默认界面是英文，示例里的 `--language zh` 会选择中文。旧的中文课堂保留原语言。要给已有配置切换界面，在它的 `config.yaml` 中把 `teacher.language` 设为 `en` 或 `zh`，然后重新打开客户端。希望老师用哪种语言讲授，也可以在课堂中直接告诉它。

## 坐下来以后

| 我想做什么 | 可以这样做 |
| --- | --- |
| 下次接着学 | 输入 `/quit` 离开。下次运行 `bin/hermes-teacher chat --resume`，原笔记和待答题都在。 |
| 答一道题 | 在 Teacher 命令行按 Tab，用方向键选答案，按 Enter 提交。文字聊天可直接发送选项字母。 |
| 讨论自己的材料 | 把 Markdown 文件放进资料库的 `materials/`，告诉老师相对路径，以及想一起读的段落。 |
| 留下这节课 | 用 `/status` 查看当前记录。Teacher 命令行的 `/seal` 封存笔记，`/organize` 把封存的记录整理成章节和复习卡。 |
| 换到桌面端 | 在 Hermes Desktop 打开 Teacher 专用配置，在「技能与工具」启用桌面插件。按 **F2** 或输入 `/classroom` 即可选课。 |

你提交的文字和材料会交给所选模型。研究角色可以访问外部来源，协作者可以在后台运行，绘图时可调用本机渲染器。资料库、测验状态和课堂原文保存在本机。飞书适配器已用合成事件验证，真实飞书投递和手机 App 体验仍待实机验证。

## 参与开发

Python 插件位于 `teacher/`，桌面和终端视图位于 `desktop/` 和 `tui/`，随插件安装的教学指南位于 `skills/`。在仓库根目录运行 `python3 -m unittest discover -s tests -v`，即可用临时课堂执行回归测试。

桌面交互测试使用 Node 24。将 `HERMES_DESKTOP_SOURCE` 设为已安装开发依赖的 Hermes `apps/desktop` 目录，然后运行 `node --test --test-isolation=none tests/desktop/quiz-plugin.test.mjs`。

## 灵感与许可

Amos Blomqvist 的 **learn** 展示了一种很有意思的学习方式：用教学 skill 带课，用小测验检查理解，需要时画一张图，再把一节课留下成 Markdown 笔记。Hermes Teacher 从中借鉴了这套教学思路，插件和教学指南面向 Hermes Agent 实现。[访问原项目](https://github.com/amosblomqvist/learn)。

Hermes Teacher 采用 [MIT 许可证](LICENSE)。
