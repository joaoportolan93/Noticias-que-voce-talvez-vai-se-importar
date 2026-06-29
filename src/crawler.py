# -*- coding: utf-8 -*-
"""
News Crawler Engine
Adapted from Meridiano's utils.py - No AI dependencies
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup

from feeds_list import FEEDS

import socket
from urllib3.util import connection

# Force IPv4 to prevent connection timeouts on Windows IPv6 resolution issues
def allowed_gai_family():
    return socket.AF_INET

connection.allowed_gai_family = allowed_gai_family

# Gemini API Configuration for search grounding fallback
# IMPORTANT: Never hardcode API keys! Use environment variables or GitHub Secrets.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    print("⚠️  WARNING: GEMINI_API_KEY not set. Gemini features (search fallback, crossword) will be disabled.")
    print("   Set it via: export GEMINI_API_KEY=your_key  (or add to .env file)")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
MAX_GEMINI_CALLS_PER_RUN = 4
gemini_calls_made = 0

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Constants
REQUEST_TIMEOUT = 15
MAX_ARTICLES_PER_FEED = 5
MAX_ARTICLES_PER_CATEGORY = 10
MAX_ARTICLE_AGE_DAYS = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Blacklist keywords for sensitive content filtering
BLACKLIST_KEYWORDS = [
    # Morte - todas as formas
    "morte",
    "morto",
    "morta",
    "mortos",
    "mortas",
    "morre",      # conjugação: ele/ela morre
    "morrem",     # conjugação: eles/elas morrem
    "morreu",     # conjugação: ele/ela morreu
    "morreram",   # conjugação: eles/elas morreram
    "morrer",     # infinitivo
    # Violência e crimes
    "assassinato",
    "assassinatos",
    "assassinado",
    "assassinada",
    "homicídio",
    "homicidio",
    "sangue",
    "estupro",
    "estuprada",
    "estuprador",
    "corpo encontrado",
    "tiroteio",
    "baleado",
    "baleada",
    "esfaqueado",
    "esfaqueada",
    "facadas",
    "atropelado",
    "atropelada",
    "atropelamento",
    # Acidentes fatais
    "afogado",
    "afogada",
    "afogados",
    "afogamento",
    "incêndio",
    "incendio",
    # Tragédias
    "tragédia",
    "tragedia",
    "massacre",
    "chacina",
    "violência",
    "violencia",
    "suicídio",
    "suicidio",
]


def contains_blacklisted_content(title: str, summary: str) -> bool:
    """
    Check if the article title or summary contains blacklisted keywords.
    
    Args:
        title: Article title
        summary: Article summary/excerpt
        
    Returns:
        True if blacklisted content is found, False otherwise
    """
    text_to_check = f"{title} {summary}".lower()
    
    for keyword in BLACKLIST_KEYWORDS:
        if keyword.lower() in text_to_check:
            logger.info(f"  ⚠ Filtered out (blacklist): '{title[:50]}...'")
            return True
    
    return False


def fetch_feed(feed_url: str) -> list:
    """
    Parse an RSS feed and return list of entries.
    
    Args:
        feed_url: URL of the RSS feed
        
    Returns:
        List of feed entries with title, link, date, author
    """
    try:
        logger.info(f"Fetching feed: {feed_url}")
        feed = feedparser.parse(feed_url)
        
        if feed.bozo and not feed.entries:
            logger.warning(f"Feed parsing error: {feed_url} - {feed.bozo_exception}")
            return []
        
        entries = []
        for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
            # Parse publication date
            pub_date = None
            pub_datetime = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                try:
                    pub_datetime = datetime(*entry.published_parsed[:6])
                    pub_date = pub_datetime.isoformat()
                except Exception:
                    pass
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                try:
                    pub_datetime = datetime(*entry.updated_parsed[:6])
                    pub_date = pub_datetime.isoformat()
                except Exception:
                    pass
            
            if not pub_date:
                pub_datetime = datetime.now()
                pub_date = pub_datetime.isoformat()
            
            # Filter out articles older than MAX_ARTICLE_AGE_DAYS days
            age_days = (datetime.now() - pub_datetime).days
            if age_days > MAX_ARTICLE_AGE_DAYS:
                logger.info(f"  ⚠ Skipped old article ({age_days} days old): '{entry.get('title', '')[:50]}...'")
                continue
            
            title = entry.get("title", "Sem título")
            summary = clean_html(entry.get("summary", ""))
            
            # Apply blacklist filter
            if contains_blacklisted_content(title, summary):
                continue
            
            entries.append({
                "title": title,
                "link": entry.get("link", ""),
                "date": pub_date,
                "author": entry.get("author", entry.get("dc_creator", "Redação")),
                "summary": summary,
            })
        
        return entries
        
    except Exception as e:
        logger.error(f"Error fetching feed {feed_url}: {e}")
        return []


def clean_html(html_text: str) -> str:
    """Remove HTML tags and clean up text."""
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "lxml")
    text = soup.get_text(separator=" ", strip=True)
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:300] if len(text) > 300 else text


def fetch_article_content_and_image(url: str) -> dict:
    """
    Fetches HTML, extracts main content using Trafilatura,
    and extracts the og:image URL using BeautifulSoup.
    
    Adapted from Meridiano's utils.py
    
    Returns:
        dict: {'content': str|None, 'og_image': str|None, 'title': str|None}
    """
    content = None
    og_image = None
    title = None
    
    try:
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        html_content = response.text
        
        # Extract text content with trafilatura
        content = trafilatura.extract(
            html_content, 
            include_comments=False, 
            include_tables=False,
            include_images=False
        )
        
        # Parse HTML for og:image and title
        soup = BeautifulSoup(html_content, "lxml")
        
        # Extract og:image
        og_image_tag = soup.find("meta", property="og:image")
        if og_image_tag and og_image_tag.get("content"):
            og_image = og_image_tag["content"]
            og_image = urljoin(url, og_image)
        
        # Fallback: try twitter:image
        if not og_image:
            twitter_img = soup.find("meta", attrs={"name": "twitter:image"})
            if twitter_img and twitter_img.get("content"):
                og_image = twitter_img["content"]
                og_image = urljoin(url, og_image)
        
        # Extract title from og:title or <title>
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            title = og_title["content"]
        elif soup.title:
            title = soup.title.string
        
        return {"content": content, "og_image": og_image, "title": title}
        
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching {url}")
        return {"content": None, "og_image": None, "title": None}
    except requests.exceptions.RequestException as e:
        logger.warning(f"Request error fetching {url}: {e}")
        return {"content": None, "og_image": None, "title": None}
    except Exception as e:
        logger.error(f"Error processing {url}: {e}")
        return {"content": content, "og_image": None, "title": None}


def calculate_read_time(text: str, wpm: int = 200) -> int:
    """Calculate estimated reading time in minutes."""
    if not text:
        return 1
    word_count = len(text.split())
    minutes = max(1, round(word_count / wpm))
    return minutes


def extract_excerpt(content: str, max_length: int = 200) -> str:
    """Extract a clean excerpt from content."""
    if not content:
        return ""
    # Clean and truncate
    excerpt = content.strip()
    if len(excerpt) > max_length:
        excerpt = excerpt[:max_length].rsplit(' ', 1)[0] + "..."
    return excerpt


def get_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except:
        return ""


def sort_articles_by_date(articles: list, descending: bool = True) -> list:
    """
    Sort articles by publication date.
    
    Args:
        articles: List of article dicts with 'date' field
        descending: If True, most recent first; if False, oldest first
        
    Returns:
        Sorted list of articles
    """
    def parse_date(article):
        try:
            return datetime.fromisoformat(article.get("date", ""))
        except (ValueError, TypeError):
            return datetime.min
    
    return sorted(articles, key=parse_date, reverse=descending)


def fetch_news_via_gemini(category_id: str, category_name: str, keywords: str) -> list:
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY environment variable is not set. Skipping Gemini Search Grounding.")
        return []
        
    global gemini_calls_made
    if gemini_calls_made >= MAX_GEMINI_CALLS_PER_RUN:
        logger.warning("Gemini API call limit reached for this run. Skipping search.")
        return []
    
    logger.info(f"🔍 Fetching news via Gemini Search Grounding for: {category_name}...")
    
    prompt = (
        f"Pesquise as últimas notícias em português sobre {category_name} (foco em: {keywords}) no Brasil. "
        "Retorne a resposta estritamente como um array JSON válido contendo até 4 objetos. "
        "Cada objeto deve conter exatamente as seguintes chaves com valores válidos: "
        "'title' (título da notícia), 'link' (URL real da notícia), 'date' (data no formato ISO YYYY-MM-DD), "
        "'author' (nome do autor ou redação), 'summary' (resumo curto de 1-2 sentenças), "
        "'content' (texto completo curto da notícia com cerca de 100-150 palavras) e "
        "'image_url' (URL da imagem principal da notícia, se disponível, caso contrário null). "
        "Não use nenhuma formatação markdown como ```json ou ``` no início/fim da resposta. Retorne apenas o JSON puro."
    )
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "tools": [
            {"googleSearch": {}}
        ]
    }
    
    try:
        gemini_calls_made += 1
        response = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=30)
        if response.status_code != 200:
            logger.error(f"Gemini API error ({response.status_code}): {response.text}")
            return []
        
        response_json = response.json()
        text_content = response_json["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        # Clean markdown codeblocks if model returned them despite instructions
        if text_content.startswith("```"):
            lines = text_content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            text_content = "\n".join(lines).strip()
            
        articles_data = json.loads(text_content)
        
        articles = []
        for entry in articles_data:
            link = entry.get("link", "")
            title = entry.get("title", "Sem título")
            
            # Apply blacklist filter
            if contains_blacklisted_content(title, entry.get("content", "")):
                continue
                
            # Filter out articles older than MAX_ARTICLE_AGE_DAYS days
            date_str = entry.get("date", "")
            is_old = False
            age_days = 0
            try:
                # Attempt to parse YYYY-MM-DD
                entry_date = datetime.strptime(date_str[:10], "%Y-%m-%d")
                age_days = (datetime.now() - entry_date).days
                if age_days > MAX_ARTICLE_AGE_DAYS:
                    is_old = True
            except Exception:
                pass
                
            if is_old:
                logger.info(f"  ⚠ Skipped old Gemini article ({age_days} days old): '{title[:50]}...'")
                continue
                
            article = {
                "id": hash(link) & 0xFFFFFFFF,
                "title": title,
                "link": link,
                "date": entry.get("date", datetime.now().isoformat()[:10]),
                "author": entry.get("author", "Redação") or "Redação",
                "category": category_id,
                "category_name": category_name,
                "image_url": entry.get("image_url"),
                "excerpt": entry.get("summary", "") or entry.get("content", "")[:200],
                "content": entry.get("content", ""),
                "read_time": max(1, round(len(entry.get("content", "").split()) / 200)),
                "domain": get_domain(link),
                "has_image": entry.get("image_url") is not None and len(str(entry.get("image_url"))) > 10,
            }
            articles.append(article)
            
        return articles
        
    except Exception as e:
        logger.error(f"Error calling or parsing Gemini API: {e}")
        return []


def build_crossword_layout(words_data):
    """
    Deterministic layout algorithm to fit chosen words into a 9x9 crossword grid.
    """
    # Sort words by length descending
    words_data = sorted(words_data, key=lambda x: len(x.get('word', '')), reverse=True)
    
    # Clean words to contain only uppercase alphabetical letters
    cleaned_words = []
    for w in words_data:
        word_text = "".join(c for c in w.get('word', '').upper() if c.isalpha())
        # Strip accents
        import unicodedata
        word_text = unicodedata.normalize('NFKD', word_text).encode('ASCII', 'ignore').decode('ASCII')
        
        if len(word_text) >= 4 and len(word_text) <= 8:
            cleaned_words.append({
                'word': word_text,
                'clue': w.get('clue', '')
            })
            
    if not cleaned_words:
        return None
        
    grid_size = 9
    grid = [[None for _ in range(grid_size)] for _ in range(grid_size)]
    placed_words = []
    
    def can_place(word, r, c, direction):
        w_len = len(word)
        if direction == 'H':
            if c + w_len > grid_size: return False
            if c > 0 and grid[r][c-1] is not None: return False
            if c + w_len < grid_size and grid[r][c+w_len] is not None: return False
            
            intersected = False
            for i in range(w_len):
                cell_r, cell_c = r, c + i
                current = grid[cell_r][cell_c]
                
                if current is not None:
                    if current['letter'] != word[i]: return False
                    intersected = True
                else:
                    # Check adjacent cells above and below
                    if cell_r > 0 and grid[cell_r-1][cell_c] is not None: return False
                    if cell_r < grid_size - 1 and grid[cell_r+1][cell_c] is not None: return False
            return True if len(placed_words) == 0 or intersected else False
            
        else: # 'V'
            if r + w_len > grid_size: return False
            if r > 0 and grid[r-1][c] is not None: return False
            if r + w_len < grid_size and grid[r+w_len][c] is not None: return False
            
            intersected = False
            for i in range(w_len):
                cell_r, cell_c = r + i, c
                current = grid[cell_r][cell_c]
                
                if current is not None:
                    if current['letter'] != word[i]: return False
                    intersected = True
                else:
                    # Check adjacent cells left and right
                    if cell_c > 0 and grid[cell_r][cell_c-1] is not None: return False
                    if cell_c < grid_size - 1 and grid[cell_r][cell_c+1] is not None: return False
            return True if len(placed_words) == 0 or intersected else False

    def place_word(word_info, r, c, direction):
        word = word_info['word']
        w_len = len(word)
        for i in range(w_len):
            cell_r, cell_c = (r, c + i) if direction == 'H' else (r + i, c)
            if grid[cell_r][cell_c] is None:
                grid[cell_r][cell_c] = {'letter': word[i], 'num': 0}
        
        placed_words.append({
            'word': word,
            'clue': word_info['clue'],
            'row': r,
            'col': c,
            'direction': direction
        })

    # Place longest word in center
    first_word_info = cleaned_words[0]
    first_word = first_word_info['word']
    start_r = grid_size // 2
    start_c = (grid_size - len(first_word)) // 2
    place_word(first_word_info, start_r, start_c, 'H')
    
    # Try placing other words on intersections
    for word_info in cleaned_words[1:]:
        word = word_info['word']
        placed = False
        
        for placed_w in placed_words:
            if placed: break
            for i, char_p in enumerate(placed_w['word']):
                if placed: break
                for j, char_n in enumerate(word):
                    if char_p == char_n:
                        if placed_w['direction'] == 'H':
                            r = placed_w['row'] - j
                            c = placed_w['col'] + i
                            direction = 'V'
                        else:
                            r = placed_w['row'] + i
                            c = placed_w['col'] - j
                            direction = 'H'
                            
                        if can_place(word, r, c, direction):
                            place_word(word_info, r, c, direction)
                            placed = True
                            break
                            
    # We want at least 3 words in the crossword grid
    if len(placed_words) < 3:
        return None
        
    clue_num = 1
    sorted_placed = sorted(placed_words, key=lambda x: (x['row'], x['col']))
    clues = {'horizontal': [], 'vertical': []}
    cell_numbers = {}
    
    for w in sorted_placed:
        coord = (w['row'], w['col'])
        if coord not in cell_numbers:
            cell_numbers[coord] = clue_num
            clue_num += 1
        
        num = cell_numbers[coord]
        grid[w['row']][w['col']]['num'] = num
        
        clue_entry = {'num': num, 'text': f"{w['clue']} ({len(w['word'])} letras)."}
        if w['direction'] == 'H':
            clues['horizontal'].append(clue_entry)
        else:
            clues['vertical'].append(clue_entry)
            
    final_grid = []
    for r in range(grid_size):
        row = []
        for c in range(grid_size):
            cell = grid[r][c]
            if cell is None:
                row.append(None)
            else:
                row.append({
                    'letter': cell['letter'],
                    'num': cell['num']
                })
        final_grid.append(row)
        
    return {
        'grid': final_grid,
        'clues': clues
    }


def generate_crossword_with_gemini(all_articles: list) -> dict:
    """
    Use Gemini API to get thematic words and clues based on today's news,
    then build a 9x9 crossword grid dynamically.
    """
    global gemini_calls_made
    
    # Default fallback crossword
    fallback_crossword = {
        "grid": [
            [None, {"letter": "J", "num": 1}, None, None, None, None, {"letter": "R", "num": 2}, None, None],
            [{"letter": "N", "num": 3}, {"letter": "O", "num": 0}, {"letter": "T", "num": 0}, {"letter": "I", "num": 0}, {"letter": "C", "num": 4}, {"letter": "I", "num": 0}, {"letter": "A", "num": 0}, {"letter": "S", "num": 0}, None],
            [None, {"letter": "R", "num": 0}, None, None, {"letter": "I", "num": 0}, None, {"letter": "D", "num": 0}, None, None],
            [None, {"letter": "N", "num": 0}, None, None, {"letter": "N", "num": 0}, None, {"letter": "I", "num": 0}, None, None],
            [{"letter": "P", "num": 5}, {"letter": "A", "num": 0}, {"letter": "R", "num": 0}, {"letter": "T", "num": 0}, {"letter": "E", "num": 0}, None, {"letter": "A", "num": 0}, None, None],
            [None, {"letter": "L", "num": 0}, None, None, {"letter": "M", "num": 0}, None, {"letter": "N", "num": 0}, None, None],
            [None, None, None, None, {"letter": "A", "num": 0}, None, {"letter": "C", "num": 0}, None, None],
            [None, None, {"letter": "P", "num": 6}, {"letter": "O", "num": 0}, {"letter": "S", "num": 0}, {"letter": "T", "num": 0}, {"letter": "E", "num": 0}, {"letter": "R", "num": 0}, None],
            [None, None, None, None, None, None, None, None, None]
        ],
        "clues": {
            "horizontal": [
                {"num": 3, "text": "Mídia ou seção de notícias atualizadas (8 letras)."},
                {"num": 5, "text": "Caderno ou divisão de um jornal impresso (5 letras)."},
                {"num": 6, "text": "Cartaz ou folheto impresso de estilo vintage (6 letras)."}
            ],
            "vertical": [
                {"num": 1, "text": "Gazeta ou diário impresso de notícias (6 letras)."},
                {"num": 2, "text": "Fonte de texto legível usada no corpo do jornal (8 letras)."},
                {"num": 4, "text": "Seção voltada para a sétima arte e lançamentos (7 letras)."}
            ]
        }
    }
    
    if not GEMINI_API_KEY:
        logger.info("GEMINI_API_KEY environment variable is not set. Using fallback crossword.")
        return fallback_crossword

    if not all_articles:
        logger.info("No articles available to generate crossword. Using fallback.")
        return fallback_crossword
        
    if gemini_calls_made >= MAX_GEMINI_CALLS_PER_RUN:
        logger.info("Gemini call limit reached. Using fallback crossword.")
        return fallback_crossword

    # Collect titles
    titles = [a.get("title", "") for a in all_articles if a.get("title")]
    titles_summary = "\n".join([f"- {t}" for t in titles[:15]])
    
    prompt = (
        "Com base nos seguintes títulos de notícias, escolha exatamente 8 palavras significativas (de 4 a 8 letras) em português. "
        "Para cada palavra, crie uma pista/dica relacionada à notícia correspondente. "
        "Retorne a resposta estritamente como um objeto JSON contendo uma lista de objetos com as chaves 'word' (a palavra em maiúsculas e sem acentos) e 'clue' (a dica). "
        "Não use nenhuma formatação markdown como ```json ou ```. Retorne apenas o JSON puro.\n\n"
        f"Notícias:\n{titles_summary}"
    )

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ]
    }
    
    try:
        gemini_calls_made += 1
        logger.info("Calling Gemini to select crossword words...")
        response = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=30)
        if response.status_code != 200:
            logger.error(f"Gemini API error selecting words ({response.status_code}): {response.text}")
            return fallback_crossword
            
        response_json = response.json()
        text_content = response_json["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        # Clean markdown codeblocks
        if text_content.startswith("```"):
            lines = text_content.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            text_content = "\n".join(lines).strip()
            
        words_data = json.loads(text_content)
        
        logger.info(f"Successfully selected {len(words_data)} candidate words. Generating layout...")
        layout = build_crossword_layout(words_data)
        
        if layout:
            logger.info("Dynamic crossword successfully generated!")
            return layout
        else:
            logger.warning("Failed to construct layout from selected words. Using fallback.")
            return fallback_crossword
            
    except Exception as e:
        logger.error(f"Failed to generate dynamic crossword via Gemini: {e}. Using fallback.")
        return fallback_crossword


def crawl_category(category_id: str, category_data: dict) -> list:
    """
    Crawl all feeds for a category and return enriched articles.
    
    Args:
        category_id: Category identifier (e.g., 'tech', 'ciencia')
        category_data: Dict with 'name', 'description', 'feeds'
        
    Returns:
        List of article dicts with full metadata
    """
    articles = []
    seen_urls = set()
    
    for feed_url in category_data["feeds"]:
        entries = fetch_feed(feed_url)
        
        for entry in entries:
            # Skip duplicates
            if entry["link"] in seen_urls:
                continue
            seen_urls.add(entry["link"])
            
            # Fetch article content and image
            logger.info(f"  Processing: {entry['title'][:50]}...")
            article_data = fetch_article_content_and_image(entry["link"])
            
            # Apply blacklist filter again with full content
            full_content = article_data.get("content") or ""
            if contains_blacklisted_content(entry["title"], full_content):
                continue
            
            # Build article object
            article = {
                "id": hash(entry["link"]) & 0xFFFFFFFF,  # Positive 32-bit hash
                "title": entry["title"],
                "link": entry["link"],
                "date": entry["date"],
                "author": entry["author"],
                "category": category_id,
                "category_name": category_data["name"],
                "image_url": article_data["og_image"],
                "excerpt": extract_excerpt(article_data["content"]) or entry["summary"],
                "content": article_data["content"],
                "read_time": calculate_read_time(article_data["content"]),
                "domain": get_domain(entry["link"]),
                "has_image": article_data["og_image"] is not None,
            }
            
            articles.append(article)
            
            # Respect rate limiting
            time.sleep(0.5)
            
            # Limit articles per category
            if len(articles) >= MAX_ARTICLES_PER_CATEGORY:
                break
        
        if len(articles) >= MAX_ARTICLES_PER_CATEGORY:
            break
    
    # Fallback to Gemini search grounding if we have fewer than 4 articles and we have keywords defined
    if len(articles) < 4 and "keywords" in category_data:
        needed = 4 - len(articles)
        logger.info(f"  ⚠ Category '{category_data['name']}' has only {len(articles)} articles. Calling Gemini fallback...")
        gemini_articles = fetch_news_via_gemini(category_id, category_data["name"], category_data["keywords"])
        
        # Avoid duplicate URLs
        seen_links = {a["link"] for a in articles}
        for ga in gemini_articles:
            if ga["link"] not in seen_links:
                articles.append(ga)
                seen_links.add(ga["link"])
                if len(articles) >= 4:
                    break
    
    # Sort articles by date (most recent first)
    articles = sort_articles_by_date(articles, descending=True)
    
    return articles


def select_capa_articles(all_articles: list, count: int = 6) -> list:
    """
    Select top articles for the front page (Capa).
    Prioritizes articles WITH images from major sources.
    """
    # First: articles with images from Capa category
    capa_with_images = [
        a for a in all_articles 
        if a["category"] == "capa" and a["has_image"]
    ]
    
    # Second: any article with images
    other_with_images = [
        a for a in all_articles 
        if a["category"] != "capa" and a["has_image"]
    ]
    
    # Combine and limit
    selected = capa_with_images[:count]
    if len(selected) < count:
        selected.extend(other_with_images[:count - len(selected)])
    
    # If still not enough, add articles without images
    if len(selected) < count:
        remaining = [a for a in all_articles if a not in selected]
        selected.extend(remaining[:count - len(selected)])
    
    # Sort selected capa articles by date (most recent first)
    selected = sort_articles_by_date(selected, descending=True)
    
    return selected[:count]


def crawl_all_feeds() -> dict:
    """
    Main crawling function. Fetches all feeds and returns structured data.
    
    Returns:
        Dict with 'capa', 'categories', and 'metadata'
    """
    logger.info("=" * 50)
    logger.info("Starting news crawl...")
    logger.info("=" * 50)
    
    start_time = time.time()
    all_articles = []
    categories_data = {}
    
    for category_id, category_info in FEEDS.items():
        logger.info(f"\n📰 Category: {category_info['name']}")
        logger.info("-" * 30)
        
        articles = crawl_category(category_id, category_info)
        all_articles.extend(articles)
        
        categories_data[category_id] = {
            "name": category_info["name"],
            "description": category_info["description"],
            "articles": articles,
            "count": len(articles),
        }
        
        logger.info(f"  ✓ Found {len(articles)} articles")
    
    # Select featured articles for Capa
    capa_articles = select_capa_articles(all_articles)
    
    # Generate daily crossword game
    crossword_game = generate_crossword_with_gemini(all_articles)
    
    elapsed = time.time() - start_time
    
    result = {
        "capa": capa_articles,
        "categories": categories_data,
        "crossword": crossword_game,
        "metadata": {
            "generated_at": datetime.now().isoformat(),
            "total_articles": len(all_articles),
            "crawl_duration_seconds": round(elapsed, 2),
            "version": "1.1.0",
        }
    }
    
    logger.info("\n" + "=" * 50)
    logger.info(f"✅ Crawl complete! {len(all_articles)} articles in {elapsed:.1f}s")
    logger.info("=" * 50)
    
    return result


def save_json(data: dict, output_path: str):
    """Save data to JSON file."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"💾 Saved to: {output_path}")


if __name__ == "__main__":
    # Quick test
    data = crawl_all_feeds()
    save_json(data, "../docs/data/latest.json")
