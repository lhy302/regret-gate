# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台（GUI 启动器）+ 内置 configs/。

关键点：
- `--windowed`：双击直接出启动器，不弹黑框；日志走文件（见 experiments/job_runner.py）；
- `configs/` 作为数据一起打包，运行时通过 `sys._MEIPASS` 定位（见 experiments/paths.py）；
- 排除明显用不到的大块依赖（numpy/scipy/pandas 等），避免体积膨胀。
"""

import os

block_cipher = None

HARNESS_DIR = os.path.abspath(os.getcwd())

datas = [
    (os.path.join(HARNESS_DIR, "configs"), "configs"),
    (os.path.join(HARNESS_DIR, "open_questions.md"), "."),
    (os.path.join(HARNESS_DIR, "README.md"), "."),
]

a = Analysis(
    [os.path.join(HARNESS_DIR, "harness_cli.py")],
    pathex=[HARNESS_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "core.types",
        "core.audit_logger",
        "core.executor",
        "core.external_auditor",
        "core.payload_builder",
        "core.pending_actions",
        "core.revision_stack",
        "core.risk_router",
        "core.stream_collector",
        "core.tail_audit",
        "core.tool_call_parser",
        "llm.base_client",
        "llm.fake_client",
        "llm.generators",
        "llm.model_list",
        "llm.openai_client",
        "llm.anthropic_client",
        "tasks.loader",
        "tasks.schema",
        "experiments.analysis",
        "experiments.gui",
        "experiments.harness",
        "experiments.job_runner",
        "experiments.metrics",
        "experiments.paths",
        "experiments.report",
        "experiments.runner",
        "experiments.stats",
        "experiments.summarize",
        "experiments.validators",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "numpy", "scipy", "pandas", "matplotlib", "PIL", "pytest",
        "PyQt5", "PySide2", "PySide6", "IPython", "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="regret-gate-harness",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无控制台：双击打开 GUI 启动器
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
