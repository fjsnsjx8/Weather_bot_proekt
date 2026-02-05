import os
import logging
import asyncio
import random
import requests
import time
from datetime import datetime
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# --- Загрузка .env ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

if not all([TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY]):
    raise ValueError("❌ Укажите TELEGRAM_BOT_TOKEN и OPENROUTER_API_KEY в .env")

# --- Настройки ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class UserState(StatesGroup):
    waiting_for_location = State()

# Константы
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "arcee-ai/trinity-large-preview:free"

# --- Вспомогательные функции ---

def ru_day_name(dt: datetime) -> str:
    days = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
    return days[dt.weekday()]

def get_forecast(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": (
            "temperature_2m_max,temperature_2m_min,weathercode,"
            "precipitation_sum,wind_speed_10m_max,relative_humidity_2m_mean,"
            "uv_index_max,sunrise,sunset"
        ),
        "timezone": "auto",
        "forecast_days": 7
    }
    for attempt in range(3):
        try:
            resp = requests.get(OPENMETEO_URL, params=params, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Попытка {attempt + 1} не удалась: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt + random.uniform(0, 1))
                continue
            else:
                raise Exception("Сервис погоды временно недоступен. Попробуйте позже.")

def extract_7_day_forecast(data: dict) -> list:
    days = []
    daily = data["daily"]
    for i in range(min(7, len(daily["time"]))):
        dt = datetime.fromisoformat(daily["time"][i])
        days.append({
            "date": dt.strftime("%d.%m"),
            "day_name": ru_day_name(dt),
            "temp_min": daily["temperature_2m_min"][i],
            "temp_max": daily["temperature_2m_max"][i],
            "condition": "прогноз погоды",
            "description": "данные с Open-Meteo",
            "humidity": round(daily["relative_humidity_2m_mean"][i]) if daily.get("relative_humidity_2m_mean") else 0,
            "wind_ms": daily["wind_speed_10m_max"][i],
            "precip_mm": daily["precipitation_sum"][i],
            "uv": daily.get("uv_index_max", [0]*7)[i],
            "sunrise": datetime.fromisoformat(daily["sunrise"][i]).strftime("%H:%M") if daily.get("sunrise") else "—",
            "sunset": datetime.fromisoformat(daily["sunset"][i]).strftime("%H:%M") if daily.get("sunset") else "—"
        })
    return days

async def get_ai_recommendation(day_info: dict) -> str:
    prompt = (
        f"Погода: {day_info['condition']}, "
        f"{day_info['temp_min']:.0f}…{day_info['temp_max']:.0f}°C, "
        f"осадки {day_info['precip_mm']} мм, "
        f"ветер {day_info['wind_ms']} м/с. "
        f"Дай 1 предложение: как лучше одеться и есть ли особые рекомендации. "
        f"1 предложение с рекомендациями по времяпровождению."
    )

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 200
    }

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    OPENROUTER_URL,
                    json=payload,
                    headers=headers,
                    timeout=60
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = data["choices"][0]["message"]["content"].strip()
                        return text.replace("*", "").replace("_", "").replace("`", "").strip('". ')
                    elif resp.status == 429:
                        delay = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(f"429 — жду {delay:.1f} сек (попытка {attempt+1}/3)")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        error = await resp.text()
                        logger.error(f"OpenRouter {resp.status}: {error[:100]}")
                        break
        except Exception as e:
            logger.warning(f"Попытка {attempt+1} не удалась: {e}")
            await asyncio.sleep(2 ** attempt)

    return "Оденьтесь по погоде и планируйте активности с учётом осадков."

# --- Хендлеры ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геопозицию", request_location=True)]],
        resize_keyboard=True
    )
    await message.answer(
        "🌤️ Привет! Отправьте свою геопозицию, чтобы получить прогноз погоды на неделю.",
        reply_markup=kb
    )
    await state.set_state(UserState.waiting_for_location)

@dp.message(UserState.waiting_for_location)
async def handle_location(message: types.Message, state: FSMContext):
    if not message.location:
        await message.answer("Пожалуйста, отправьте геопозицию с помощью кнопки 📍")
        return

    await message.answer("⏳ Получаю прогноз...", reply_markup=ReplyKeyboardRemove())
    lat, lon = message.location.latitude, message.location.longitude

    try:
        raw = get_forecast(lat, lon)
        days = extract_7_day_forecast(raw)

        for day in days:
            rec = await get_ai_recommendation(day)
            precip = f", 🌧️ {day['precip_mm']} мм" if day["precip_mm"] > 0 else ""
            uv_emoji = "☀️" if day["uv"] > 5 else "🌤️"
            text = (
                f"{uv_emoji} {day['day_name']}, {day['date']}\n"
                f"🌡️ {day['temp_min']:.0f}…{day['temp_max']:.0f}°C{precip}\n"
                f"💨 {day['wind_ms']} м/с | 💧 {day['humidity']}% | УФ: {day['uv']}\n"
                f"🌅 {day['sunrise']} → 🌇 {day['sunset']}\n"
                f"📌 Рекомендация: {rec}"
            )
            await message.answer(text)
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.exception("Ошибка")
        await message.answer(f"❌ {e}")

# --- Запуск ---
async def main():
    logger.info("✅ Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())