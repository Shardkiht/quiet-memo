"""Textual 主题与布局样式；ChatApp 通过 CSS = APP_CSS 应用。"""
from __future__ import annotations

APP_CSS = """
    $accent: #d2d3d7;
    $dim: #3a3b42;
    $muted: #96979c;
    $faint: #63646b;
    $panel: #17181c;
    $bg: #131417;
    $scroll-track: #131417;
    $scroll-thumb: #45464e;
    $scroll-thumb-hover: #63646b;
    $scroll-thumb-active: #83848c;

    Screen { background: $bg; }
    /* 统一窄滚动条；新的可滚动组件加 thin-scroll 类即可复用。 */
    .thin-scroll {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: $scroll-track;
        scrollbar-color: $scroll-thumb;
        scrollbar-color-hover: $scroll-thumb-hover;
        scrollbar-color-active: $scroll-thumb-active;
    }
    #root { width: 1fr; height: 1fr; }
    #sidebar { width: 28; background: #101114; border-right: solid #202127; }
    #sidebar-head { height: 3; align: left middle; }
    #brand { color: $accent; text-style: bold; padding: 0 1; width: 1fr; }
    /* 折叠按钮只留符号，减少侧栏头部的方框噪音 */
    #hide-sidebar { width: 3; height: 3; min-width: 3; padding: 0; background: #101114; color: $faint; border: none; margin-right: 1; content-align: center middle; }
    #hide-sidebar:hover { color: $accent; }
    #show-sidebar { layer: overlay; dock: top; width: 3; height: 3; min-width: 3; padding: 0; background: $bg; color: $faint; border: none; content-align: center middle; display: none; }
    #show-sidebar:hover { color: $accent; }
    #new-session { width: 1fr; height: 3; margin: 0 1 1 1; background: #17181c; color: $muted; border: none; content-align: left middle; padding: 0 1; }
    #new-session:hover { background: #202127; color: $accent; }
    /* 列表与 New session 用同一左右外边距；隐藏滚动条，避免出现滚动条时行宽缩 2 格错位 */
    #sessions { width: 1fr; height: 1fr; margin: 0 1; scrollbar-size-vertical: 0; }
    /* ListItem 默认高度跟随子容器（Vertical 是 1fr 会撑满列表），必须钉死 auto */
    #sessions > ListItem { padding: 0 1; height: auto; }
    #sessions > ListItem > Vertical { height: auto; }
    .session-title { color: #c2c3c7; width: 1fr; overflow-x: hidden; text-overflow: ellipsis; }
    .session-meta { color: #777980; }
    /* 当前会话只用一条左线和轻微底色，避免整行亮色像误选 */
    #sessions > ListItem.-highlight { background: #1c1d22; }
    #sessions > ListItem.current { background: #1b1c21; border-left: heavy $muted; }
    #sessions > ListItem.current.-highlight { background: #22242a; border-left: heavy $accent; }
    .group-label { color: #777980; padding: 1 1 0 1; }

    #main { width: 1fr; layers: base overlay; }
    #chat { width: 100%; height: 1fr; padding: 1 0 1 3; }
    /* 右侧留白改由子元素承担，滚动条才能贴在窗口最右侧 */
    #chat > * { margin-right: 2; }
    .who-user { content-align: right top; color: #7c7d84; width: 1fr; margin-top: 1; }
    .user-turn { width: 1fr; height: auto; align: right top; }
    .bubble { background: #1e2026; color: #d5d6da; padding: 0 2; width: auto; max-width: 68%; height: auto; }
    .who-ai { color: $accent; text-style: bold; margin-top: 1; width: 1fr; }
    .reply { color: #d0d1d5; margin: 0; height: auto; }
    /* 工具事件用薄边线和弱底色分组，不与正式回复争夺注意力 */
    .event-card { border-left: solid $dim; background: #15161a; color: $muted; padding: 0 1; width: 1fr; }
    .event-card.memo { border-left: solid $muted; color: #a5a6ac; }
    .meta { color: $faint; width: 1fr; }
    .divider { border-top: solid #24262c; height: 1; margin: 1 0; }
    .card { border-left: solid $dim; background: $panel; padding: 0 2; margin: 1 0; color: #c2c3c7; width: 1fr; }
    /* 空会话欢迎区：无边框，靠字号和留白建立层级 */
    .welcome-brand { color: #d5d6da; text-style: bold; margin: 2 0 0 0; }
    .welcome-line { color: #9a9b9f; }
    .welcome-meta { color: #777980; margin-top: 1; }
    .welcome-hint { color: #777980; margin-top: 2; }

    /* 输入框不做焦点整块提亮，避免像额外遮罩；只用边框区分焦点。 */
    #input { margin: 0 3 1 3; background: $bg; border: round #303138; border-title-color: #83848c; border-title-align: left; }
    #input:focus { border: round #56575f; border-title-color: #b0b1b6; }
    /* 光标由 BarInput 渲染成竖线；这里只提供前景色，避免默认整格反色块。 */
    #input > .input--cursor {
        background: transparent;
        color: #d5d6da;
    }
    /* 1 行正文 + 1 行上边框；height:1 会被边框吃掉整个内容区。 */
    #status { width: 1fr; height: 2; background: $bg; color: $faint; padding: 0 3; border-top: solid #1d1e23; }

    /* 命令面板：浮在输入框上方，只显示命令、参数和一句短语。
       -6 = status 2 行 + 输入框下边距 1 行 + 输入框 3 行；偏移不够会盖住输入框。 */
    #cmd-palette { layer: overlay; dock: bottom; offset-x: 3; offset-y: -6; width: 64; height: auto; max-height: 16; background: $panel; border: round #383a42; padding: 0 1; display: none; }
    .cmd-row { width: 1fr; height: 1; padding: 0 1; color: #b9babd; }
    .cmd-row.cmd-active { background: #292b32; }
    .cmd-usage { color: #96979c; }
    .cmd-desc { color: #7d7e85; }

    PickScreen, SessionPickerScreen, TextInputScreen { align: center middle; background: #0d0e11 75%; }
    #pick-card { width: 64; height: auto; max-height: 70%; background: $panel; border: round #383a42; padding: 1 2; }
    #picker-input, #pick-search, #pick-input { width: 1fr; background: #131417; color: #d5d6da; border: round #303138; }
    #picker-input:focus, #pick-search:focus, #pick-input:focus { background: #191a1f; border: round #56575f; }
    #picker-input, #pick-search { margin-bottom: 1; }
    #picker-list { height: auto; max-height: 16; }
    /* 与侧栏一致：SessionItem 内层 Vertical 默认 1fr 会把整行撑满 */
    #picker-list > ListItem { padding: 0 1; height: auto; border-left: heavy transparent; }
    #picker-list > ListItem > Vertical { height: auto; }
    #picker-list > ListItem.-highlight { background: #22242a; border-left: heavy $accent; }
    #picker-list > ListItem.current { background: #1b1c21; border-left: heavy $muted; }
    #picker-list > ListItem.current.-highlight { background: #22242a; border-left: heavy $accent; }
    .pick-title { color: $accent; text-style: bold; margin-bottom: 1; }
    #pick-list { height: auto; max-height: 18; }
    #pick-list > ListItem { height: auto; }
    #pick-list > ListItem.-highlight { background: #292b32; }
    .pick-hint { color: #83848c; margin-top: 1; }
    """
