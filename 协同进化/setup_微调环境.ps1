# =====================================================================
# Qwen3 微调环境一键搭建
# 目标：建独立 venv + CUDA 版 torch + 训练依赖 + HF 镜像
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File setup_微调环境.ps1
#
# 注意：本文件必须以「UTF-8 带 BOM」保存。
#   PowerShell 5.1 在中文系统上会把无 BOM 的 UTF-8 当 GBK 读，
#   导致中文路径解析失败、脚本报莫名的语法错误。
# =====================================================================

$ErrorActionPreference = "Stop"

$root = "D:\工作区表\工作区4\协同进化"
$venv = Join-Path $root ".venv-finetune"
$py   = Join-Path $venv "Scripts\python.exe"

$MIRROR_PYPI = "https://pypi.tuna.tsinghua.edu.cn/simple"
$TORCH_CUDA  = "https://download.pytorch.org/whl/cu121"
$HF_MIRROR   = "https://hf-mirror.com"

Write-Host "====================================================="
Write-Host " Qwen3 微调环境搭建"
Write-Host "====================================================="
Write-Host ""

# ---------- 0. 前置检查 ----------
Write-Host "[0/5] 前置检查"

# huggingface.co 在国内通常不可达，必须先设镜像，否则后面会卡住
if ($env:HF_ENDPOINT -ne $HF_MIRROR) {
    Write-Host "      huggingface.co 通常不可达，设置 HF_ENDPOINT -> $HF_MIRROR"
}

$basePython = $null
foreach ($cand in @("python", "py")) {
    $c = Get-Command $cand -ErrorAction SilentlyContinue
    if ($c) { $basePython = $c.Source; break }
}
if (-not $basePython) {
    Write-Host "[错误] 找不到 python，请先安装 Python 3.10+"
    exit 1
}
Write-Host "      基础解释器: $basePython"
& $basePython --version
Write-Host ""

# ---------- 1. 建 venv ----------
Write-Host "[1/5] 建立独立虚拟环境（与 GGUF 转换环境隔离）"
if (Test-Path $py) {
    Write-Host "      已存在，跳过: $venv"
} else {
    & $basePython -m venv $venv
    if (-not (Test-Path $py)) {
        Write-Host "[错误] venv 创建失败"
        exit 1
    }
    Write-Host "      已创建: $venv"
}
& $py -V
Write-Host ""

# ---------- 2. CUDA 版 torch ----------
# 关键：PyPI 上的 torch 默认是 CPU 版，必须指定 PyTorch 官方 CUDA 索引。
# cu121 对 Pascal (compute_cap 6.1) 仍然可用。
Write-Host "[2/5] 安装 CUDA 版 torch（cu121）"
Write-Host "      注意：这一步下载量较大（约 2.5 GB），请耐心等待"
& $py -m pip install --upgrade pip --quiet `
    --index-url $MIRROR_PYPI 2>&1 | Select-Object -Last 2
& $py -m pip install torch --index-url $TORCH_CUDA
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] torch 安装失败。可重试，或改用 cu118 索引："
    Write-Host "       --index-url https://download.pytorch.org/whl/cu118"
    exit 1
}
Write-Host ""

# ---------- 3. 训练依赖 ----------
Write-Host "[3/5] 安装训练依赖"
& $py -m pip install --index-url $MIRROR_PYPI `
    transformers datasets accelerate peft trl bitsandbytes sentencepiece `
    modelscope huggingface_hub safetensors
if ($LASTEXITCODE -ne 0) {
    Write-Host "[警告] 部分依赖安装失败（bitsandbytes 在 Windows 上偶有问题）"
    Write-Host "       若只做 LoRA 不做 QLoRA，可以没有 bitsandbytes"
}
Write-Host ""

# ---------- 4. HF 镜像持久化 ----------
Write-Host "[4/5] 持久化 HF 镜像设置"
[Environment]::SetEnvironmentVariable("HF_ENDPOINT", $HF_MIRROR, "User")
$env:HF_ENDPOINT = $HF_MIRROR
Write-Host "      已写入用户环境变量 HF_ENDPOINT=$HF_MIRROR"
Write-Host "      （新开的终端自动生效；当前终端也已设置）"
Write-Host ""

# ---------- 5. 验证 ----------
Write-Host "[5/5] 验证环境"
Write-Host "--- torch / CUDA ---"
& $py -c @"
import torch
print('torch        :', torch.__version__)
print('cuda 可用    :', torch.cuda.is_available())
if torch.cuda.is_available():
    print('GPU          :', torch.cuda.get_device_name(0))
    print('显存 (GB)    :', round(torch.cuda.get_device_properties(0).total_memory/1024**3, 2))
    print('compute cap  :', torch.cuda.get_device_capability(0))
else:
    print('!!! 装成了 CPU 版，训练会极慢。请重装 cu121 版 torch')
"@

Write-Host "--- 训练库 ---"
& $py -c @"
mods = ['transformers','datasets','accelerate','peft','trl','bitsandbytes','sentencepiece','modelscope','huggingface_hub']
for m in mods:
    try:
        mod = __import__(m)
        print(f'  {m:18} OK  {getattr(mod, \"__version__\", \"?\")}')
    except Exception as e:
        print(f'  {m:18} 缺失 ({type(e).__name__})')
"@

Write-Host ""
Write-Host "====================================================="
Write-Host " 完成"
Write-Host "====================================================="
Write-Host " venv      : $venv"
Write-Host " python    : $py"
Write-Host " HF 镜像   : $HF_MIRROR"
Write-Host ""
Write-Host " 下一步："
Write-Host "   1. 确认上面 cuda 可用 = True"
Write-Host "   2. 按 微调环境搭建.md §4 下载 Qwen3-4B 对照模型"
Write-Host "   3. 按 微调环境搭建.md §5 先用官方 qlora yaml 打通链路"
Write-Host ""
Write-Host " ⚠️ 本机显存仅 4 GB，务必用 QLoRA + gradient_checkpointing + 短序列"
Write-Host "    详见 微调环境搭建.md §3"
Write-Host "====================================================="
