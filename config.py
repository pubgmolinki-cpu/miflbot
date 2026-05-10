import os

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
AI_API_KEY = os.getenv("AI_API_KEY") # Ключ для нейросети (Gemini/OpenAI)
CHANNEL_ID = "@Miflcards"
ADMIN_IDS = [5185444605]

RARITY_BOOSTS = {
    "Stock": 0.05,
    "Series": 0.15,
    "Drop": 0.30,
    "Chase": 0.50,
    "One": 1.00
}

RARITY_RANKS = {"Stock": 1, "Series": 2, "Drop": 3, "Chase": 4, "One": 5}
