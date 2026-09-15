# PowerShell script to download models for Chinese CLIP Embedding Service
# Usage: .\scripts\download_models.ps1 [-UseMirror] [-ModelDir "path"]

param(
    [switch]$UseMirror = $false,
    [string]$ModelDir = "",
    [switch]$SkipCLIP = $false,
    [switch]$SkipBGE = $false
)

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Chinese CLIP Embedding Service - Model Downloader" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# Determine model directory
if ($ModelDir -eq "") {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $ModelDir = Join-Path (Split-Path -Parent $ScriptDir) "models"
}

$ModelDir = Resolve-Path $ModelDir -ErrorAction SilentlyContinue
if (-not $ModelDir) {
    $ModelDir = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "models"
    New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
}

Write-Host "Model directory: $ModelDir" -ForegroundColor Yellow
Write-Host "Use mirror: $UseMirror" -ForegroundColor Yellow
Write-Host ""

# Setup mirror if requested
if ($UseMirror) {
    $env:HF_ENDPOINT = "https://hf-mirror.com"
    Write-Host "✓ Using HF-Mirror (https://hf-mirror.com) for faster downloads" -ForegroundColor Green
}

$Success = $true

# Download Chinese CLIP
if (-not $SkipCLIP) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "Downloading Chinese CLIP ViT-Huge-Patch14" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    
    try {
        python -c "from huggingface_hub import snapshot_download; snapshot_download('OFA-Sys/chinese-clip-vit-huge-patch14', local_dir='$ModelDir', local_dir_use_symlinks=False, resume_download=True, allow_patterns=['pytorch_model.bin', 'config.json', 'preprocessor_config.json', 'vocab.txt'])"
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✓ Chinese CLIP model downloaded successfully!" -ForegroundColor Green
        } else {
            Write-Host "❌ Error downloading Chinese CLIP" -ForegroundColor Red
            $Success = $false
        }
    } catch {
        Write-Host "❌ Error: $_" -ForegroundColor Red
        Write-Host "Please install huggingface_hub: pip install huggingface-hub" -ForegroundColor Yellow
        $Success = $false
    }
} else {
    Write-Host ""
    Write-Host "⊘ Skipping Chinese CLIP download" -ForegroundColor Yellow
}

# Download BGE
if (-not $SkipBGE) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "Downloading BGE Large ZH v1.5" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    
    try {
        python -c "from transformers import AutoTokenizer, AutoModel; AutoTokenizer.from_pretrained('BAAI/bge-large-zh-v1.5'); AutoModel.from_pretrained('BAAI/bge-large-zh-v1.5')"
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "✓ BGE model downloaded successfully!" -ForegroundColor Green
            Write-Host "  Cached in: ~/.cache/huggingface/hub/" -ForegroundColor Gray
        } else {
            Write-Host "❌ Error downloading BGE" -ForegroundColor Red
            $Success = $false
        }
    } catch {
        Write-Host "❌ Error: $_" -ForegroundColor Red
        Write-Host "Please install transformers: pip install transformers" -ForegroundColor Yellow
        $Success = $false
    }
} else {
    Write-Host ""
    Write-Host "⊘ Skipping BGE download" -ForegroundColor Yellow
}

# Verify files
if (-not $SkipCLIP) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host "Verifying Model Files" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    
    $RequiredFiles = @(
        "pytorch_model.bin",
        "config.json",
        "preprocessor_config.json",
        "vocab.txt"
    )
    
    foreach ($File in $RequiredFiles) {
        $FilePath = Join-Path $ModelDir $File
        if (Test-Path $FilePath) {
            $SizeMB = [math]::Round((Get-Item $FilePath).Length / 1MB, 2)
            Write-Host "✓ $File ($SizeMB MB)" -ForegroundColor Green
        } else {
            Write-Host "✗ $File (missing)" -ForegroundColor Red
            $Success = $false
        }
    }
}

# Summary
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
if ($Success) {
    Write-Host "✓ All models downloaded successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Yellow
    Write-Host '1. Start: $env:EMBEDDING_SERVICE_PORT="8030"; python start_embedding_service.py' -ForegroundColor White
    Write-Host "2. Open docs: http://127.0.0.1:8030/docs" -ForegroundColor White
    Write-Host "3. Health:  curl http://127.0.0.1:8030/health" -ForegroundColor White
} else {
    Write-Host "❌ Some downloads failed. Please check the errors above." -ForegroundColor Red
}
Write-Host "============================================================" -ForegroundColor Cyan
