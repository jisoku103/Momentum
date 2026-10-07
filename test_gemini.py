import asyncio
import os
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

print(f"Testing Gemini API...")
print(f"Model: {model_name}")
print(f"API Key: {api_key[:6]}... (length: {len(api_key) if api_key else 0})")

async def test_call():
    client = genai.Client(api_key=api_key)
    start = time.time()
    try:
        response = await client.aio.models.generate_content(
            model=model_name,
            contents="こんにちは。1+1の答えだけを答えてください。",
        )
        elapsed = time.time() - start
        print(f"✅ 成功! 応答時間: {elapsed:.2f}秒")
        print(f"レスポンス: {response.text}")
    except Exception as e:
        elapsed = time.time() - start
        print(f"❌ 失敗 (経過時間: {elapsed:.2f}秒)")
        print(f"エラー詳細: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(test_call())