#!/usr/bin/env python3
"""
Run the bot server directly without uvicorn reload
"""
import uvicorn

if __name__ == "__main__":
    print("=" * 60)
    print("🤖 Adjarabet Bot Server Starting...")
    print("=" * 60)
    print("📍 Server will run on: http://localhost:8000")
    print("📍 Press CTRL+C to stop")
    print("=" * 60)
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,  # Important! Don't use reload with Playwright
        log_level="info"
    )