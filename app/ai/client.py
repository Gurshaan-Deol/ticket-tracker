import json
import logging

from app.config import get_settings

logger = logging.getLogger(__name__)


def _get_client():
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None
    settings = get_settings()
    if not settings.ai_api_key:
        return None
    return AsyncOpenAI(
        api_key=settings.ai_api_key,
        base_url=settings.ai_base_url or None,
    )


async def parse_event_search(query: str) -> dict | None:
    """
    Parses a natural-language concert search query into structured JSON.
    Returns {"keyword": str, "city": str, "date_hint": str} or None.
    """
    client = _get_client()
    if not client:
        return None
    settings = get_settings()
    try:
        response = await client.chat.completions.create(
            model=settings.ai_model or "gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You parse concert search queries into structured JSON. "
                        "Always respond with only valid JSON, no markdown, no explanation. "
                        'Format: {"keyword": "artist name", "city": "city name or empty string", '
                        '"date_hint": "date description or empty string"}'
                    ),
                },
                {"role": "user", "content": query},
            ],
            max_tokens=100,
            temperature=0,
        )
        text = response.choices[0].message.content.strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("AI parse_event_search failed: %s", e)
        return None


async def summarize_price_history(snapshots: list[dict]) -> str | None:
    """
    Summarizes a list of {"price": float, "scraped_at": str} dicts in plain English.
    Returns None if AI is not configured or the call fails.
    """
    client = _get_client()
    if not client:
        return None
    if not snapshots:
        return None
    settings = get_settings()
    try:
        data_str = json.dumps(snapshots[-50:])
        response = await client.chat.completions.create(
            model=settings.ai_model or "gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You analyze ticket price history data and write a 2-3 sentence "
                        "plain English summary of the trend. Be specific about percentages "
                        "and timing. No markdown, just plain text."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Summarize this price history: {data_str}",
                },
            ],
            max_tokens=150,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("AI summarize_price_history failed: %s", e)
        return None
