py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev,vision]"
Write-Host "Install COLMAP and Vulkan drivers, then run scripts/setup_vksplat.sh under Git Bash or clone VkSplat manually."
