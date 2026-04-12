#!/usr/bin/env bash

# Install Playwright browsers
echo "Installing Playwright browsers..."
python -m playwright install chromium
python -m playwright install-deps

echo "Build completed!"