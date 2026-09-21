"""desktop 打包脚本回归：Windows PATHEXT 解析风险（P0 批次一）。

npm script 中的裸 ``electron-builder`` 命令在 Windows 上按 PATHEXT 顺序解析，
``.JS`` 扩展可能抢在 npm 生成的 ``.cmd`` shim 之前命中，由错误的解释器执行
导致打包失败。CI（release-win7.yml）已改用 ``node node_modules/electron-builder/cli.js``
直接调用本地 CLI；``dist:win`` 必须与 CI 同构，并显式加载
``electron-builder.win.js``（Windows 签名配置）。

验证方式（无测试替身）：json 标准库解析真实 desktop/package.json，
断言脚本形状与被引用配置文件的真实存在。
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_PATH = REPO_ROOT / "desktop" / "package.json"
WIN_CONFIG_PATH = REPO_ROOT / "desktop" / "electron-builder.win.js"
HELPER_PATH = REPO_ROOT / "desktop" / "scripts" / "run-electron-builder.mjs"
DESKTOP_DIR = REPO_ROOT / "desktop"
HELPER_OUT_MARKER = "__HELPER_OUT__"

# CI 的可靠调用方式（release-win7.yml「Build installer」步骤同构）。
_EXPECTED_INVOCATION = "node node_modules/electron-builder/cli.js --win --x64 --config electron-builder.win.js"


def _scripts() -> dict:
    return json.loads(PKG_PATH.read_text(encoding="utf-8"))["scripts"]


def test_dist_win_invokes_local_cli_via_node_and_loads_win_config():
    script = _scripts()["dist:win"]
    assert script.endswith(_EXPECTED_INVOCATION), (
        f"dist:win 必须以 CI 同构方式调用本地 CLI 并显式加载 Windows 签名配置，当前为: {script!r}"
    )
    # 被引用的 Windows 签名配置必须是真实存在的文件，而不是悬空引用。
    assert WIN_CONFIG_PATH.is_file(), (
        f"dist:win 引用的 electron-builder.win.js 不存在: {WIN_CONFIG_PATH}"
    )


def test_dist_win_contains_no_bare_electron_builder_command():
    script = _scripts()["dist:win"]
    # 裸命令 = PATH 上按 PATHEXT 解析的 electron-builder 词元。
    # 排除路径内部出现（node_modules/electron-builder/cli.js）与显式配置文件名。
    for token in re.findall(r"[\w./\\:-]*electron-builder[\w./\\:-]*", script):
        if "/" in token or "\\" in token or token.endswith(".js"):
            continue
        raise AssertionError(
            f"dist:win 含裸 electron-builder 命令（Windows PATHEXT 风险）: {token!r}"
        )


def test_all_packaging_scripts_avoid_bare_electron_builder():
    # P1 补充：不止 dist:win——README 推荐的 npm run dist、dist:flavor 以及
    # dist:mac 都可能在 Windows 上被敲出来，而当前目录 desktop/electron-builder.js
    # 在 PATHEXT=.JS 解析顺序中先于 node_modules/.bin shim，任何裸命令都会
    # 启动错误程序。四个打包脚本统一要求显式 node 本地 CLI（与 CI release.yml
    # 的调用方式一致）。
    scripts = _scripts()
    for name in ("dist", "dist:mac", "dist:win", "dist:flavor"):
        assert name in scripts, f"缺少打包脚本 {name}"
        script = scripts[name]
        for token in re.findall(r"[\w./\\:-]*electron-builder[\w./\\:-]*", script):
            if "/" in token or "\\" in token or token.endswith(".js"):
                continue
            raise AssertionError(
                f"{name} 含裸 electron-builder 命令（Windows PATHEXT 风险）: {token!r}"
            )


def _node_eval(module_path: Path, code: str) -> dict:
    """真实 node 子进程加载真实模块并回传 JSON 结果（无替身）。

    模块路径经环境变量传入而非 argv：helper 的 import guard 以
    ``process.argv[1] === 模块自身`` 判定"作为主程序执行"，argv 带模块
    路径会误触 main() 真的启动 electron-builder 构建。
    """
    import os

    proc = subprocess.run(
        ["node", "-e", code],
        cwd=str(DESKTOP_DIR),
        env={**os.environ, "SA_NODE_MODULE": str(module_path)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith(HELPER_OUT_MARKER):
            return json.loads(line[len(HELPER_OUT_MARKER):])
    raise AssertionError(
        f"node 子进程无结果输出 (exit={proc.returncode}): {proc.stderr[-500:]}"
    )


def test_dist_routes_through_platform_config_helper():
    # P1：PATHEXT 只是通道问题。默认入口还必须与 dist:win 同效——README
    # 推荐的 npm run dist 在 Windows 上要加载 electron-builder.win.js（签名
    # hook + rg.exe resource），否则制品与 dist:win 不等价（隐式配置下
    # 两者均为 false）。dist 经平台配置 helper 调用 electron-builder。
    script = _scripts()["dist"]
    assert script.endswith("node scripts/run-electron-builder.mjs"), (
        f"dist 必须经平台配置 helper 调用 electron-builder，当前为: {script!r}"
    )


def test_electron_builder_helper_selects_win_config_only_on_windows():
    # helper 的平台选择逻辑（真实 node 子进程加载真实模块执行）：
    # win32 → 显式 --config 指向真实存在的 electron-builder.win.js，且
    # 保留透传参数；非 Windows → 保持隐式配置（行为与改动前一致）。
    code = (
        "const { pathToFileURL } = require('node:url');"
        "import(pathToFileURL(process.env.SA_NODE_MODULE).href).then((m) => {"
        "  const win = m.buildElectronBuilderArgs('win32', ['--dir']);"
        "  const darwin = m.buildElectronBuilderArgs('darwin', ['--dir']);"
        "  console.log('__HELPER_OUT__' + JSON.stringify({win: win, darwin: darwin}));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = _node_eval(HELPER_PATH, code)

    assert out["win"][0] == "--dir", "透传参数必须保留"
    assert "--config" in out["win"], "win32 必须显式加载 Windows 专用配置"
    win_cfg = Path(out["win"][out["win"].index("--config") + 1])
    assert win_cfg.name == "electron-builder.win.js"
    assert win_cfg.is_file(), f"--config 指向的文件不存在: {win_cfg}"
    assert "--config" not in out["darwin"], "非 Windows 保持隐式配置"


def test_win_config_wires_sign_hook_and_rg_resource():
    # 锁定 effective config 内容（真实 node 子进程 require 真实配置模块）：
    # electron-builder.win.js 无条件注入 win.signtoolOptions.sign 签名
    # hook 与 rg.exe extraResource——这正是隐式 package.json build 字段
    # 所缺的两项，也是 dist 必须加载它的原因。
    code = (
        "const { pathToFileURL } = require('node:url');"
        "import(pathToFileURL(process.env.SA_NODE_MODULE).href).then((m) => {"
        "  const cfg = m.default ?? m;"
        "  const sign = cfg.win && cfg.win.signtoolOptions && cfg.win.signtoolOptions.sign;"
        "  const rg = (cfg.extraResources || []).some((r) => Array.isArray(r.filter) && r.filter.indexOf('rg.exe') >= 0);"
        "  console.log('__HELPER_OUT__' + JSON.stringify({sign: typeof sign, rg: rg}));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = _node_eval(WIN_CONFIG_PATH, code)

    assert out["sign"] == "function", "win 配置必须注入 signtoolOptions.sign 签名 hook"
    assert out["rg"] is True, "win 配置必须注入 rg.exe extraResource"


def test_electron_builder_helper_rejects_explicit_config_on_windows():
    # P1 回归：electron-builder 只认最后一个 --config。Windows 分支在
    # passthrough 之后追加自己的 --config electron-builder.win.js，用户
    # 传入的显式配置会被静默忽略（实测 custom.yml 失效）。必须明确拒绝
    # 而非静默覆盖；非 Windows 平台不追加配置，透传不受影响。
    code = (
        "const { pathToFileURL } = require('node:url');"
        "import(pathToFileURL(process.env.SA_NODE_MODULE).href).then((m) => {"
        "  const tryCase = (platform, args) => {"
        "    try { return { threw: false, args: m.buildElectronBuilderArgs(platform, args) }; }"
        "    catch (e) { return { threw: true, message: String(e.message) }; }"
        "  };"
        "  const r1 = tryCase('win32', ['--config', 'custom.yml']);"
        "  const r2 = tryCase('win32', ['--config=custom.yml']);"
        "  const r3 = tryCase('win32', ['-c', 'custom.yml']);"
        "  const r4 = tryCase('win32', ['--dir']);"
        "  const r5 = tryCase('darwin', ['--config', 'custom.yml']);"
        "  console.log('__HELPER_OUT__' + JSON.stringify({r1: r1, r2: r2, r3: r3, r4: r4, r5: r5}));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = _node_eval(HELPER_PATH, code)

    for key in ("r1", "r2", "r3"):
        assert out[key]["threw"] is True, (
            f"{key}: win32 下显式 --config 必须被拒绝，而非静默覆盖"
        )
        assert "--config" in out[key]["message"], "拒绝信息必须指明 --config 冲突原因"
    # 无显式配置时 Windows 行为保持：追加 win 配置 + 保留透传
    assert out["r4"]["threw"] is False
    assert out["r4"]["args"][0] == "--dir"
    assert "--config" in out["r4"]["args"]
    # 非 Windows：不追加、不拒绝，透传原样
    assert out["r5"]["threw"] is False
    assert out["r5"]["args"] == ["--config", "custom.yml"]


def test_electron_builder_helper_rejects_all_compact_short_config_forms():
    # P1 回归：yargs 把单划线 token 按字符拆成短旗标，'c' 是 config 选项；
    # 分隔符 =、@、/ 出现在 c 之后时还会把剩余部分粘成配置值（实测
    # -c@custom.yml、-c/custom.yml 曾绕过只匹配四个字面量的检测，builder
    # 只认最后追加的 win.js，用户配置被静默丢弃）。字面量枚举不可靠，
    # 按"含 c 的单划线 token"整体拒绝。
    code = (
        "const { pathToFileURL } = require('node:url');"
        "import(pathToFileURL(process.env.SA_NODE_MODULE).href).then((m) => {"
        "  const forms = ['-c=custom.yml', '-c@custom.yml', '-c/custom.yml', '-ccustom.yml', '-c'];"
        "  const cases = forms.map((f) => {"
        "    try { m.buildElectronBuilderArgs('win32', [f, 'extra.yml']); return {form: f, threw: false}; }"
        "    catch (e) { return {form: f, threw: true, message: String(e.message)}; }"
        "  });"
        "  console.log('__HELPER_OUT__' + JSON.stringify({cases: cases}));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = _node_eval(HELPER_PATH, code)

    for case in out["cases"]:
        assert case["threw"] is True, (
            f"紧凑形式 {case['form']} 必须被识别为显式配置并拒绝"
        )
        assert "--config" in case["message"]


def test_electron_builder_helper_stops_config_scan_at_double_dash():
    # P2 回归：yargs 把裸 `--` 之后的 token 解析为位置参数，不视为配置。
    # 因此：1) `--` 之后的 --config 不再触发拒绝（它不是配置）；2) 追加的
    # win 配置必须插在 `--` 之前——否则解析器同样会把它当位置参数丢弃，
    # Windows 制品静默失去签名 hook 与 rg.exe。附带洞：旧实现 append 在
    # passthrough 末尾，`--` 存在时 win 配置必被吞。
    code = (
        "const { pathToFileURL } = require('node:url');"
        "import(pathToFileURL(process.env.SA_NODE_MODULE).href).then((m) => {"
        "  const tryCase = (platform, args) => {"
        "    try { return { threw: false, args: m.buildElectronBuilderArgs(platform, args) }; }"
        "    catch (e) { return { threw: true, message: String(e.message) }; }"
        "  };"
        "  const sep = tryCase('win32', ['--', '--config', 'custom.yml']);"
        "  const sepBad = tryCase('win32', ['-c@custom.yml', '--', 'positional']);"
        "  const sepDarwin = tryCase('darwin', ['--', '--config', 'custom.yml']);"
        "  console.log('__HELPER_OUT__' + JSON.stringify({sep: sep, sepBad: sepBad, sepDarwin: sepDarwin}));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = _node_eval(HELPER_PATH, code)

    # `--` 之后的 --config 是位置参数：不拒绝，且追加配置插在 `--` 之前
    assert out["sep"]["threw"] is False, "`--` 之后的 --config 不应触发拒绝"
    sep_args = out["sep"]["args"]
    assert sep_args[0] == "--config", "win 配置必须插在 `--` 分隔符之前"
    assert sep_args[1].endswith("electron-builder.win.js")
    assert sep_args.index("--") == 2, "裸 `--` 与其后的位置参数必须原样保留在后"
    assert sep_args[2:] == ["--", "--config", "custom.yml"]
    # `--` 之前的紧凑形式仍要拒绝
    assert out["sepBad"]["threw"] is True
    # 非 Windows：无追加，透传原样
    assert out["sepDarwin"]["threw"] is False
    assert out["sepDarwin"]["args"] == ["--", "--config", "custom.yml"]


def test_electron_builder_helper_rejects_config_in_short_flag_groups():
    # P1 回归：yargs 把单划线 token 按字符拆成短旗标组合，'c' 无论在组合
    # 的哪个位置都是 config 旗标（实测 -wc@custom.yml 解析出
    # config="@custom.yml"，-mwc=custom.yml、-wlc/custom.yml 同理；
    # 分隔符 =、@、/ 还会把剩余部分粘成值）。只匹配 token 开头的 /^-c/
    # 会漏检组合形式。c 是唯一以 c 为含义的短旗标，因此任何含 'c' 的
    # 单划线 token（分隔符之前）都视为显式配置。
    code = (
        "const { pathToFileURL } = require('node:url');"
        "import(pathToFileURL(process.env.SA_NODE_MODULE).href).then((m) => {"
        "  const forms = ['-wc@custom.yml', '-mwc=custom.yml', '-wlc/custom.yml', '-cw', '-cFILE'];"
        "  const cases = forms.map((f) => {"
        "    try { m.buildElectronBuilderArgs('win32', [f]); return {form: f, threw: false}; }"
        "    catch (e) { return {form: f, threw: true, message: String(e.message)}; }"
        "  });"
        "  let clean = null;"
        "  try { clean = m.buildElectronBuilderArgs('win32', ['-wm']); } catch (e) { clean = null; }"
        "  console.log('__HELPER_OUT__' + JSON.stringify({cases: cases, clean: clean}));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = _node_eval(HELPER_PATH, code)

    for case in out["cases"]:
        assert case["threw"] is True, (
            f"短旗标组合 {case['form']} 中的 c 必须被识别为显式配置"
        )
        assert "--config" in case["message"]
    # 不含 c 的合法组合（w=win, m=mac）不受影响，win 配置照常追加
    assert out["clean"] is not None, "不含 c 的短旗标组合不应被拒绝"
    assert out["clean"][0] == "-wm"
    assert "--config" in out["clean"]


def test_helper_main_process_rejects_short_flag_group_config():
    # 评审复现的主进程路径：node run-electron-builder.mjs -wc@custom.yml
    # --version 修复前放行并返回 25.1.8/exit 0，用户配置被静默丢弃；
    # 修复后必须在 spawn 前拒绝。携带 --version 保证检测回归时也只打印
    # 版本、绝不启动构建。
    proc = subprocess.run(
        ["node", str(HELPER_PATH), "-wc@custom.yml", "--version"],
        cwd=str(DESKTOP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 1, (
        f"主进程应 exit 1，实际 {proc.returncode}；stdout={proc.stdout[-200:]}"
    )
    combined = proc.stderr + proc.stdout
    assert "cannot be passed through" in combined
    assert "-wc@custom.yml" in combined, "拒绝信息应指明触发的具体 token"


def test_electron_builder_helper_rejects_double_dash_c_alias_and_all_config_entrances():
    # P1 回归：yargs 为 config 声明了单字母 alias 'c'（builder.js），因此
    # 双划线 --c VALUE、--c=VALUE 也是合法 config 入口，曾绕过只匹配
    # --config 的检测。同一解析器还接受 --no-config/--no-c（config=false，
    # 被追加配置覆盖）与 --config.k=v/--c.k=v（dot-notation 配置对象）。
    # 策略：config 的全部 yargs 入口一律拒绝。--cscLink 等其他 c 开头的
    # 长选项不得误伤。
    code = (
        "const { pathToFileURL } = require('node:url');"
        "import(pathToFileURL(process.env.SA_NODE_MODULE).href).then((m) => {"
        "  const reject = ['--c', '--c=custom.yml', '--no-config', '--no-c',"
        "                  '--config.foo=bar', '--c.foo=bar'];"
        "  const cases = reject.map((f) => {"
        "    try { m.buildElectronBuilderArgs('win32', [f]); return {form: f, threw: false}; }"
        "    catch (e) { return {form: f, threw: true, message: String(e.message)}; }"
        "  });"
        "  let csc = null;"
        "  try { csc = m.buildElectronBuilderArgs('win32', ['--cscLink', 'cert.pfx']); } catch (e) { csc = null; }"
        "  console.log('__HELPER_OUT__' + JSON.stringify({cases: cases, csc: csc}));"
        "}).catch((e) => { console.error(e); process.exit(1); });"
    )
    out = _node_eval(HELPER_PATH, code)

    for case in out["cases"]:
        assert case["threw"] is True, (
            f"{case['form']} 是 config 的合法 yargs 入口，必须被拒绝"
        )
        assert "--config" in case["message"]
    # --cscLink 等非 config 的 c 开头长选项不受影响
    assert out["csc"] is not None, "--cscLink 不应被误拒"
    assert out["csc"][0] == "--cscLink"
    assert "--config" in out["csc"]


def test_helper_main_process_rejects_double_dash_c_alias():
    # 评审复现：node run-electron-builder.mjs --c custom.yml --version
    # 修复前 exit 0（yargs 解析出 config=["custom.yml", win.js]，用户配置
    # 被静默丢弃）。携带 --version 保证检测回归时不会启动构建。
    proc = subprocess.run(
        ["node", str(HELPER_PATH), "--c", "custom.yml", "--version"],
        cwd=str(DESKTOP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 1, (
        f"主进程应 exit 1，实际 {proc.returncode}；stdout={proc.stdout[-200:]}"
    )
    combined = proc.stderr + proc.stdout
    assert "cannot be passed through" in combined
    assert "--c" in combined, "拒绝信息应指明触发的具体 token"


def test_helper_main_process_rejects_config_and_exits_before_spawn():
    # 主进程验证（此前只测了导入后的参数函数）：紧凑绕过形式经真实
    # `node run-electron-builder.mjs` 主进程执行必须 exit 1 并给出原因。
    # 携带 --version 保证即使检测回归也只是打印版本，绝不会启动构建。
    proc = subprocess.run(
        ["node", str(HELPER_PATH), "-c@custom.yml", "--version"],
        cwd=str(DESKTOP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 1, (
        f"主进程应 exit 1，实际 {proc.returncode}；stdout={proc.stdout[-200:]}"
    )
    combined = proc.stderr + proc.stdout
    assert "cannot be passed through" in combined, "必须输出明确的拒绝原因"
    assert "electron-builder.win.js" in combined


def test_helper_main_process_version_passes_through():
    # 主进程正常通道：--version 经真实 CLI 打印版本、exit 0（无构建）。
    # 需要 desktop/node_modules（干净 checkout 不安装 npm 依赖时跳过，
    # 不计为 PASS）；拒绝类主进程用例在 spawn 前退出，不受此限制。
    cli_js = DESKTOP_DIR / "node_modules" / "electron-builder" / "cli.js"
    if not cli_js.is_file():
        pytest.skip("本 checkout 未安装 desktop/node_modules（npm ci 后可运行）")

    proc = subprocess.run(
        ["node", str(HELPER_PATH), "--version"],
        cwd=str(DESKTOP_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-200:]
    import re as _re

    assert _re.search(r"\d+\.\d+", proc.stdout), f"应打印版本号: {proc.stdout!r}"


def test_dist_flavor_routes_through_platform_config_helper():
    # dist:flavor 是 Windows 可达的打包脚本（与 dist 同类隐患：隐式配置
    # 无签名 hook 与 rg.exe）。取证：flavors/ 目录不存在、仓库内无调用方，
    # 正式 flavored Windows 制品走 CI release-overlay.yml 的 build-overlay.mjs；
    # 本地脚本仍统一改走平台 helper，保证任何入口产出的 Windows 制品同效。
    script = _scripts()["dist:flavor"]
    assert "build && node scripts/run-electron-builder.mjs &&" in script, (
        f"dist:flavor 的 electron-builder 环节必须经平台配置 helper，当前为: {script!r}"
    )
