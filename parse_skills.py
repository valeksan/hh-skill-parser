#!/usr/bin/env python3

import argparse
import csv
import functools
import html
import json
import logging
import math
import os
import random
import re
import sys
import time
from collections import Counter

import requests
from bs4 import BeautifulSoup

try:
    from console_animation import animate
except ImportError:
    def animate(*_args, **_kwargs):
        """Fallback no-op decorator when console-animation is unavailable."""
        def decorator(func):
            return func
        return decorator

try:
    from matplotlib import pyplot
except ImportError:
    pyplot = None

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_CLIENT_CONTACT = ""
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_PAGE_DELAY_MIN = 2.0
DEFAULT_PAGE_DELAY_MAX = 5.0
DEFAULT_VACANCY_DELAY_MIN = 1.0
DEFAULT_VACANCY_DELAY_MAX = 3.0
DEFAULT_DATA_SOURCE = "auto"
HTML_SEARCH_PAGE_SIZE = 20
DEFAULT_HTML_DESCRIPTION_FALLBACK = False
DEFAULT_NO_CHART = False

session = None
REQUEST_TIMEOUT = DEFAULT_REQUEST_TIMEOUT
PAGE_DELAY_MIN = DEFAULT_PAGE_DELAY_MIN
PAGE_DELAY_MAX = DEFAULT_PAGE_DELAY_MAX
VACANCY_DELAY_MIN = DEFAULT_VACANCY_DELAY_MIN
VACANCY_DELAY_MAX = DEFAULT_VACANCY_DELAY_MAX

# Вывести уже обработанные данные (для отладки, не парсить так как долго)
OPTION_SKIP_PARSING = False


class ProxyUnavailableError(RuntimeError):
    """Прокси указан, но недоступен."""


class BadUserAgentError(RuntimeError):
    """HH отверг заголовок HH-User-Agent."""


class SourceBlockedError(RuntimeError):
    """Основной источник данных заблокирован внешней защитой."""


def parse_bootstrap_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    """Считывает аргументы, нужные до основного разбора CLI."""
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--env-file",
        default=os.environ.get("HH_ENV_FILE", ".env"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_known_args(argv)


def strip_wrapping_quotes(value: str) -> str:
    """Удаляет внешние кавычки из значения .env."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_dotenv_file(path: str, override: bool = False) -> bool:
    """Загружает переменные окружения из .env-подобного файла."""
    if not path or not os.path.exists(path):
        return False

    loaded = 0
    with open(path, encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                logger.warning(
                    "Пропускаю строку %s в %s: ожидался формат KEY=VALUE",
                    line_number,
                    path,
                )
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = strip_wrapping_quotes(value.strip())

            if not key:
                logger.warning(
                    "Пропускаю строку %s в %s: пустое имя переменной",
                    line_number,
                    path,
                )
                continue

            if override or key not in os.environ:
                os.environ[key] = value
                loaded += 1

    logger.info("Загружен .env-файл %s (%s переменных)", path, loaded)
    return True


def is_ddos_guard_response(response: requests.Response | None) -> bool:
    """Проверяет, что ответ пришёл через ddos-guard."""
    if response is None:
        return False
    server = response.headers.get("server", "")
    return "ddos-guard" in server.lower()


def build_hh_user_agent(contact: str) -> str:
    """Формирует идентификатор клиента в формате, удобном для HH API."""
    contact = contact.strip() if contact else DEFAULT_CLIENT_CONTACT
    return f"hh-skill-parser/1.0 ({contact})"


def is_bad_hh_user_agent_response(response: requests.Response | None) -> bool:
    """Проверяет, что HH отклонил заголовок HH-User-Agent."""
    if response is None:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return any(error.get("type") == "bad_user_agent" for error in payload.get("errors", []))


def is_local_proxy(proxy_url: str | None) -> bool:
    """Определяет, что прокси указывает на localhost."""
    if not proxy_url:
        return False
    return "127.0.0.1" in proxy_url or "localhost" in proxy_url


def sleep_between_requests(delay_min: float, delay_max: float) -> None:
    """Спит случайное время в заданном диапазоне."""
    time.sleep(random.uniform(delay_min, delay_max))


def fetch_html(url: str, params: dict | None = None) -> str:
    """Выполняет HTML-запрос к HH без заголовка HH-User-Agent."""
    if params is None:
        params = {}

    headers = dict(session.headers)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers.pop("HH-User-Agent", None)

    response = session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def extract_vacancy_id(value: str | None) -> str | None:
    """Извлекает ID вакансии из URL или строки."""
    if not value:
        return None
    match = re.search(r"/vacancy/(\d+)", value)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{6,})\b", value)
    if match:
        return match.group(1)
    return None


def build_html_vacancy_url(vacancy_id: str) -> str:
    """Строит канонический URL HTML-страницы вакансии."""
    return f"https://hh.ru/vacancy/{vacancy_id}"


def annotate_api_vacancies(items: list[dict]) -> list[dict]:
    """Добавляет служебные поля к вакансиям, пришедшим из API."""
    annotated_items = []
    for item in items:
        vacancy = dict(item)
        vacancy["_source"] = "api"
        vacancy.setdefault("alternate_url", build_html_vacancy_url(str(vacancy["id"])))
        annotated_items.append(vacancy)
    return annotated_items


def parse_html_search_results(html_text: str) -> list[dict]:
    """Извлекает вакансии из HTML-страницы поиска HH."""
    soup = BeautifulSoup(html_text, "html.parser")
    vacancies = []
    seen_ids = set()

    for link in soup.select('a[data-qa="serp-item__title"]'):
        href = link.get("href")
        vacancy_id = extract_vacancy_id(href)
        if not vacancy_id or vacancy_id in seen_ids:
            continue

        seen_ids.add(vacancy_id)
        vacancies.append(
            {
                "id": vacancy_id,
                "name": link.get_text(" ", strip=True),
                "alternate_url": href,
                "_source": "html",
            }
        )

    return vacancies


def extract_key_skills_from_html_payload(html_text: str) -> list[str]:
    """Пытается извлечь keySkills из встроенного JSON страницы вакансии."""
    match = re.search(
        r'"keySkills":(null|\[.*?\]),"driverLicenseTypes"',
        html_text,
        re.S,
    )
    if not match or match.group(1) == "null":
        return []

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    key_skills = []
    for item in payload:
        if isinstance(item, str):
            key_skills.append(item)
            continue
        if isinstance(item, dict):
            for key in ("name", "text", "value", "label"):
                value = item.get(key)
                if value:
                    key_skills.append(value)
                    break

    return key_skills


def parse_html_vacancy_page(html_text: str, vacancy: dict) -> dict:
    """Преобразует HTML-страницу вакансии в унифицированный словарь."""
    soup = BeautifulSoup(html_text, "html.parser")

    title_node = soup.select_one('[data-qa="vacancy-title"]') or soup.find("h1")
    description_node = soup.select_one('[data-qa="vacancy-description"]')
    ld_json_node = soup.select_one('script[type="application/ld+json"]')

    description_html = ""
    if description_node is not None:
        description_html = description_node.decode_contents()
    elif ld_json_node is not None:
        try:
            ld_json_payload = json.loads(ld_json_node.get_text())
            description_html = html.unescape(ld_json_payload.get("description", ""))
        except json.JSONDecodeError:
            description_html = ""

    key_skills = extract_key_skills_from_html_payload(html_text)

    return {
        "id": str(vacancy["id"]),
        "name": (
            title_node.get_text(" ", strip=True)
            if title_node is not None
            else vacancy.get("name", "")
        ),
        "_source": "html",
        "description": description_html,
        "key_skills": [{"name": skill} for skill in key_skills],
    }


def configure_http_session(settings) -> None:
    """Создаёт и настраивает HTTP-сессию для запросов к HH API."""
    global session
    global REQUEST_TIMEOUT
    global PAGE_DELAY_MIN
    global PAGE_DELAY_MAX
    global VACANCY_DELAY_MIN
    global VACANCY_DELAY_MAX

    if settings.page_delay_min > settings.page_delay_max:
        raise ValueError("--page-delay-min не может быть больше --page-delay-max")
    if settings.vacancy_delay_min > settings.vacancy_delay_max:
        raise ValueError("--vacancy-delay-min не может быть больше --vacancy-delay-max")
    if settings.request_timeout <= 0:
        raise ValueError("--request-timeout должен быть больше нуля")

    REQUEST_TIMEOUT = settings.request_timeout
    PAGE_DELAY_MIN = settings.page_delay_min
    PAGE_DELAY_MAX = settings.page_delay_max
    VACANCY_DELAY_MIN = settings.vacancy_delay_min
    VACANCY_DELAY_MAX = settings.vacancy_delay_max

    session = requests.Session()
    headers = {
        "User-Agent": settings.browser_user_agent,
        "Accept": "application/json",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://hh.ru/search/vacancy",
        "Origin": "https://hh.ru",
    }
    if settings.send_hh_user_agent and settings.client_contact.strip():
        headers["HH-User-Agent"] = build_hh_user_agent(settings.client_contact)
    session.headers.update(headers)

    if settings.proxy:
        session.proxies.update(
            {
                "http": settings.proxy,
                "https": settings.proxy,
            }
        )

    logger.info(
        "HTTP-сессия настроена: timeout=%.1fs, page_delay=%.1f-%.1fs, vacancy_delay=%.1f-%.1fs, proxy=%s",
        REQUEST_TIMEOUT,
        PAGE_DELAY_MIN,
        PAGE_DELAY_MAX,
        VACANCY_DELAY_MIN,
        VACANCY_DELAY_MAX,
        "yes" if settings.proxy else "no",
    )


def retry_request(max_retries=3, base_delay=1.0, max_delay=30.0):
    """
    Декоратор для повторных попыток сетевых запросов с экспоненциальной задержкой.
    
    Обрабатывает:
    - requests.exceptions.RequestException (сетевые ошибки, таймауты)
    - HTTP ошибки 429 (Too Many Requests), 403 (Forbidden) и 5xx (серверные ошибки)
    
    Args:
        max_retries (int): Максимальное количество попыток (включая первую).
        base_delay (float): Базовая задержка в секундах для экспоненциального отката.
        max_delay (float): Максимальная задержка в секундах.
    
    Returns:
        Декоратор функции.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as e:
                    last_exception = e
                    # Проверяем, является ли ошибка HTTP ошибкой, которую стоит повторить
                    retry_allowed = True
                    delay = base_delay * (2 ** attempt)  # экспоненциальный откат
                    delay = min(delay, max_delay)

                    if isinstance(e, requests.exceptions.ProxyError):
                        retry_allowed = False
                        logger.error(
                            "ProxyError: не удалось подключиться к прокси. "
                            "Проверьте --proxy / HTTPS_PROXY / HTTP_PROXY или запустите без прокси."
                        )
                    
                    if hasattr(e, 'response') and e.response is not None:
                        status = e.response.status_code
                        if status == 429:
                            # Проверяем заголовок Retry-After
                            retry_after = e.response.headers.get('Retry-After')
                            if retry_after:
                                try:
                                    # Может быть числом секунд или датой в формате HTTP-date
                                    delay = float(retry_after)
                                except ValueError:
                                    # Если это дата, пропускаем и используем экспоненциальную задержку
                                    pass
                            logger.warning(f"HTTP 429 Too Many Requests. Попытка {attempt + 1}/{max_retries}. Задержка {delay:.1f}с")
                        elif status == 403:
                            # Ошибка доступа - повторяем с увеличенной задержкой
                            logger.warning(f"HTTP 403 Forbidden. Попытка {attempt + 1}/{max_retries}. Задержка {delay:.1f}с")
                            # Логируем тело ответа для диагностики
                            try:
                                body = e.response.text[:500]  # первые 500 символов
                                logger.warning(f"Тело ответа 403: {body}")
                            except:
                                pass
                            if is_ddos_guard_response(e.response):
                                logger.warning(
                                    "Ответ пришёл через ddos-guard. Обычно это означает внешний антибот-фильтр по IP/репутации клиента, "
                                    "и одни только повторы запроса могут не помочь."
                                )
                        elif status >= 500:
                            logger.warning(f"HTTP {status} Server Error. Попытка {attempt + 1}/{max_retries}. Задержка {delay:.1f}с")
                        else:
                            # Клиентские ошибки 4xx (кроме 429 и 403) не повторяем
                            retry_allowed = False
                            logger.error(f"HTTP {status} Client Error. Не повторяем.")
                            try:
                                body = e.response.text[:500]
                                logger.error(f"Тело ответа {status}: {body}")
                            except:
                                pass
                            if status == 400 and is_bad_hh_user_agent_response(e.response):
                                logger.error(
                                    "HH отклонил заголовок HH-User-Agent как bad_user_agent. "
                                    "Запустите без --send-hh-user-agent."
                                )
                    else:
                        # Сетевая ошибка (таймаут, соединение и т.д.)
                        logger.warning(f"Сетевая ошибка: {type(e).__name__}. Попытка {attempt + 1}/{max_retries}. Задержка {delay:.1f}с")
                    
                    if attempt == max_retries - 1 or not retry_allowed:
                        break
                    
                    time.sleep(delay)
                except Exception as e:
                    # Другие ошибки (не сетевые) не повторяем
                    logger.error(f"Непредвиденная ошибка: {type(e).__name__}. Не повторяем.")
                    raise
            
            # Если исчерпаны все попытки, пробрасываем последнее исключение
            raise last_exception
        return wrapper
    return decorator


@animate(start="Поиск вакансий API")
def get_vacancies_from_api(query: str, area: int, vacancies_limit: int = 2000) -> list:
    """
    Собирает вакансии с HH.ru по заданному запросу.

    Args:
        query: Поисковый запрос (например, 'data scientist', 'machine learning').
        area: ID региона (например, 1 — Москва).
        vacancies_limit: Ограничение по количеству вакансий (1–2000, ограничение API HH).

    Returns:
        Список вакансий в формате JSON (каждая вакансия — словарь).
        Возвращает пустой список в случае ошибки или отсутствия вакансий.

    Raises:
        Exception: Если vacancies_limit не является натуральным числом.
    """
    logger.debug(f"enter get_vacancies({locals()})")
    # API: https://api.hh.ru/openapi/redoc#tag/Poisk-vakansij/operation/get-vacancies
    base_url = "https://api.hh.ru/vacancies"
    params = {
        "text": query,
        "area": area,
        "per_page": 100,
        "page": 0,
        "search_field": "name",
    }

    # Ограничение кол-ва запросов
    pages_total = 0
    if vacancies_limit > 2000:
        logger.warning(
            "Ограничение по количеству вакансий API hh.ru в 2000. "
            "Выбрано максимально допустимое значение"
        )
        vacancies_limit = 2000
    elif vacancies_limit <= 0:
        logger.critical("vacancies_limit должно быть натуральным числом")
        raise Exception("vacancies_limit должно быть натуральным числом")
    pages_total = math.ceil(vacancies_limit / params["per_page"])

    all_vacancies = []
    for page_current in range(pages_total):
        params["page"] = page_current
        try:
            data = fetch_data(base_url, params)
            items = data.get("items", [])

            if not items:
                break
            all_vacancies.extend(annotate_api_vacancies(items))

            logger.info(f"Обработана страница {page_current + 1} по запросу '{query}'")
            # задержка чтоб не быть забанеными сервером удаленным, не наглеем :)
            sleep_between_requests(PAGE_DELAY_MIN, PAGE_DELAY_MAX)

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при запросе. Error: {e}")
            if isinstance(e, requests.exceptions.ProxyError):
                logger.error(
                    "Пагинация остановлена: прокси недоступен. "
                    "Скрипт не будет продолжать запросы к следующим страницам."
                )
                raise ProxyUnavailableError(
                    "Не удалось подключиться к прокси. Проверьте --proxy / HTTPS_PROXY / HTTP_PROXY."
                ) from e
            if hasattr(e, "response") and e.response is not None:
                if e.response.status_code == 400 and is_bad_hh_user_agent_response(e.response):
                    logger.error(
                        "Сбор списка вакансий остановлен: HH отверг HH-User-Agent. "
                        "Уберите --send-hh-user-agent или переменную HH_SEND_USER_AGENT."
                    )
                    raise BadUserAgentError(
                        "HH отверг заголовок HH-User-Agent. Уберите --send-hh-user-agent или HH_SEND_USER_AGENT."
                    ) from e
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 403 and is_ddos_guard_response(e.response):
                raise SourceBlockedError(
                    "Сбор списка вакансий через API остановлен из-за внешней блокировки ddos-guard."
                ) from e
            continue

    return all_vacancies


@animate(start="Поиск вакансий HTML")
def get_vacancies_from_html(query: str, area: int, vacancies_limit: int = 2000) -> list:
    """Собирает вакансии с HTML-страницы поиска HH."""
    search_url = "https://hh.ru/search/vacancy"
    if vacancies_limit <= 0:
        logger.critical("vacancies_limit должно быть натуральным числом")
        raise Exception("vacancies_limit должно быть натуральным числом")

    pages_total = math.ceil(vacancies_limit / HTML_SEARCH_PAGE_SIZE)
    all_vacancies = []

    for page_current in range(pages_total):
        params = {
            "text": query,
            "area": area,
            "page": page_current,
        }

        html_text = fetch_html(search_url, params=params)
        items = parse_html_search_results(html_text)
        if not items:
            break

        all_vacancies.extend(items)
        if len(all_vacancies) >= vacancies_limit:
            return all_vacancies[:vacancies_limit]

        logger.info(f"Обработана HTML-страница {page_current + 1} по запросу '{query}'")
        sleep_between_requests(PAGE_DELAY_MIN, PAGE_DELAY_MAX)

    return all_vacancies[:vacancies_limit]


def get_vacancies(
    query: str,
    area: int,
    vacancies_limit: int = 2000,
    source: str = DEFAULT_DATA_SOURCE,
) -> list:
    """Собирает вакансии через API, HTML или автоматический fallback."""
    if source == "api":
        return get_vacancies_from_api(query, area, vacancies_limit)
    if source == "html":
        return get_vacancies_from_html(query, area, vacancies_limit)

    try:
        return get_vacancies_from_api(query, area, vacancies_limit)
    except SourceBlockedError as error:
        logger.warning("%s Переключаюсь на HTML fallback.", error)
        return get_vacancies_from_html(query, area, vacancies_limit)
    except BadUserAgentError as error:
        logger.warning("%s Переключаюсь на HTML fallback.", error)
        return get_vacancies_from_html(query, area, vacancies_limit)


def fetch_vacancy_data_from_html(vacancy: dict) -> dict:
    """Загружает HTML-страницу вакансии и нормализует её структуру."""
    vacancy_id = str(vacancy["id"])
    url = vacancy.get("alternate_url") or build_html_vacancy_url(vacancy_id)
    html_text = fetch_html(url)
    return parse_html_vacancy_page(html_text, vacancy)


def fetch_vacancy_data(vacancy: dict, source: str = DEFAULT_DATA_SOURCE) -> dict:
    """Загружает данные вакансии с учётом выбранного источника."""
    vacancy_source = vacancy.get("_source", "api")
    vacancy_id = str(vacancy["id"])

    if source == "html" or vacancy_source == "html":
        return fetch_vacancy_data_from_html(vacancy)

    api_url = f"https://api.hh.ru/vacancies/{vacancy_id}"
    try:
        data = fetch_data(api_url)
        data["_source"] = "api"
        return data
    except requests.exceptions.RequestException as error:
        if source != "auto":
            raise

        response = getattr(error, "response", None)
        if response is not None:
            if response.status_code == 400 and is_bad_hh_user_agent_response(response):
                logger.warning(
                    "API деталей вакансии отверг HH-User-Agent. Переключаюсь на HTML для вакансии %s.",
                    vacancy_id,
                )
                return fetch_vacancy_data_from_html(vacancy)
            if response.status_code == 403 and is_ddos_guard_response(response):
                logger.warning(
                    "API деталей вакансии заблокирован ddos-guard. Переключаюсь на HTML для вакансии %s.",
                    vacancy_id,
                )
                return fetch_vacancy_data_from_html(vacancy)

        raise


def resolve_processing_mode(settings, vacancy_data: dict) -> str:
    """Выбирает фактический режим обработки для конкретной вакансии."""
    if (
        settings.mode == "key-skills"
        and settings.html_description_fallback
        and vacancy_data.get("_source") == "html"
    ):
        return "description"
    return settings.mode


def load_skills_whitelist(path: str = "skills_whitelist.txt") -> set:
    """
    Загружает белый список навыков из файла.

    Строки, начинающиеся с '#', считаются комментариями и игнорируются.
    Пустые строки также пропускаются. Все навыки приводятся к нижнему регистру.

    Args:
        path: Путь к файлу со списком навыков. По умолчанию "skills_whitelist.txt".

    Returns:
        Множество навыков (в нижнем регистре).

    Raises:
        FileNotFoundError: Если файл не найден.
        Exception: Если файл не может быть загружен.
    """
    logger.debug(f"enter load_skills_whitelist({locals()})")
    try:
        with open(path, encoding="utf-8") as f:
            lines = [
                line.strip().lower()
                for line in f
                if line.strip() and not line.startswith("#")
            ]
        return set(lines)

    except FileNotFoundError:
        logger.warning(f"Not found {path}")
        raise Exception("Can't load skills_whitelist")


def extract_skills(text: str, skill_whitelist: set | list) -> list:
    """
    Извлекает навыки из текста (например, описание вакансии) с использованием
    регулярных выражений с границами слов.

    Производит поиск по переданному списку навыков (skill_whitelist),
    регистронезависимо. Поддерживает многословные навыки (например, 'computer vision'),
    сортировка которых происходит по длине, чтобы избежать частичного совпадения
    (например, 'vision' внутри 'computer vision').

    Args:
        text (str): Текст (например, описание вакансии).
        skill_whitelist (set or list): Множество или список допустимых навыков.

    Returns:
        list: Список найденных навыков (в нижнем регистре).
    """
    logger.debug("enter extract_skills(can't show to much data)")
    text_lower = text.lower()
    found_skills = []
    
    # Нормализация навыков: удаление лишних пробелов, экранирование для regex
    normalized_skills = []
    for skill in skill_whitelist:
        # Удаляем лишние пробелы по краям и внутри (заменяем множественные пробелы на один)
        norm = re.sub(r'\s+', ' ', skill.strip())
        normalized_skills.append(norm)
    
    # Сортируем по длине в обратном порядке, чтобы более длинные навыки обрабатывались первыми
    for skill in sorted(normalized_skills, key=len, reverse=True):
        # Экранируем специальные символы для regex
        pattern = re.escape(skill)
        # Используем гибкие границы слов: перед навыком не должно быть буквенно-цифрового символа,
        # после навыка тоже не должно быть буквенно-цифрового символа.
        # Это позволяет корректно обрабатывать навыки с дефисами, точками, плюсами и т.д.
        if re.search(r'(?<!\w)' + pattern + r'(?!\w)', text_lower):
            found_skills.append(skill)
            # Удаляем найденный навык из текста, чтобы избежать повторного обнаружения
            # (заменяем на пробел, но сохраняем границы)
            text_lower = re.sub(r'(?<!\w)' + pattern + r'(?!\w)', ' ', text_lower)
    
    return found_skills


def load_queries(path: str = "queries.txt") -> list:
    """
    Загружает список поисковых запросов (названий вакансий) из файла.

    Строки, начинающиеся с '#', считаются комментариями и игнорируются.
    Пустые строки также пропускаются.

    Args:
        path: Путь к файлу со списком запросов. По умолчанию "queries.txt".

    Returns:
        Список строк — запросов для поиска вакансий.

    Raises:
        FileNotFoundError: Если файл не найден.
        Exception: Если файл не может быть загружен.
    """
    logger.debug(f"enter load_queries({locals()})")
    try:
        with open(path, encoding="utf-8") as f:
            lines = [
                line.strip() for line in f if line.strip() and not line.startswith("#")
            ]
        queries = [query for query in lines if query]
        return queries
    except FileNotFoundError:
        logger.critical(f"Not found file {path}")
        raise Exception("Can't load query")


def save_progress(data: dict, file_path: str = "progress.json") -> None:
    """
    Сохраняет текущий прогресс в JSON-файл.

    Args:
        data: Словарь с данными прогресса.
        file_path: Путь к файлу для сохранения. По умолчанию "progress.json".
    """
    logger.debug(f"enter save_progress({locals()})")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_progress(path: str = "progress.json") -> dict:
    """
    Загружает прогресс из JSON-файла, если существует.

    Args:
        path: Путь к файлу прогресса. По умолчанию "progress.json".

    Returns:
        Словарь с данными прогресса или пустой словарь, если файл не существует.
    """
    logger.debug(f"enter load_progress({locals()})")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def cli_parse(argv: list[str] | None = None):
    """
    Парсит аргументы командной строки.

    Returns:
        argparse.Namespace:
            area (int): Зона поиска (API HH)
            output (str): Целевой файл для записи конечного результата
            vacancies_limit (int):
                Ограничение кол-ва вакансий на каждый поисковой запрос
            skills_show_count (int):
                Ограничение кол-ва навыков для отображения в конечном
                результате (графике)
    """
    logger.debug(f"enter cli_parse({locals()})")
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "Программа собирает вакансии с сайта https://hh.ru по ключевым "
            'запросам (например, "DataScience", "ML Engineer"), извлекает '
            "из них **навыки**, и строит столбчатую диаграмму самых "
            "популярных навыков"
            "\n\n"
            "конфигурационные файлы:\n"
            "  queries.txt — список ключевых запросов для поиска вакансий\n"
            "  skills_whitelist.txt — список навыков для анализа [только для --mode description]"
        ),
        epilog=(
            "Примеры использования:\n"
            "  python parse_skills.py -a 1 -o skills.png\n"
            "  python parse_skills.py --mode description --skills-show-count 30\n"
            "  python parse_skills.py --vacancies-limit 1000 --save-every 20\n"
            "\n"
            "Для получения справки используйте --help или -h."
        ),
        add_help=True,
    )

    parser.add_argument(
        "-o",
        "--output",
        default="hh_skills_bar_chart.png",
        help="Файл для вывода результата (график) (%(default)s)",
    )

    parser.add_argument(
        "-a",
        "--area",
        type=int,
        default=1,
        help=(
            "ID города/региона поиска вакансий. "
            "Найти можно тут -> https://api.hh.ru/areas (по умолчанию %(default)s)"
        ),
    )

    parser.add_argument(
        "--vacancies-limit",
        dest="vacancies_limit",
        type=int,
        default=2000,
        help="Ограничение на количество вакансий для обработки на каждый запрос (%(default)s)",
    )

    parser.add_argument(
        "--skills-show-count",
        "--skills-count",
        type=int,
        default=50,
        help="Количество отображаемых навыков в графике (%(default)s)",
    )

    parser.add_argument(
        "-m",
        "--mode",
        default="key-skills",
        choices=["key-skills", "description"],
        help=(
            "Режим key-skills: извлекает навыки из поля key_skills вакансии, "
            "не требует skills_whitelist.txt\n"
            "Режим description: анализирует текст описания вакансий, "
            "требует файл skills_whitelist.txt (%(default)s)"
        ),
    )

    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        help="Сохранять прогресс каждые N обработанных вакансий (%(default)s)",
    )

    parser.add_argument(
        "--client-contact",
        default=os.environ.get("HH_CLIENT_CONTACT", DEFAULT_CLIENT_CONTACT),
        help=(
            "Контакт для HH-User-Agent: email, URL проекта или Telegram "
            "(используется только вместе с --send-hh-user-agent)"
        ),
    )

    parser.add_argument(
        "--send-hh-user-agent",
        action="store_true",
        default=os.environ.get("HH_SEND_USER_AGENT", "").lower() in {"1", "true", "yes"},
        help="Отправлять заголовок HH-User-Agent. По умолчанию выключено, так как API может отклонять его как bad_user_agent.",
    )

    parser.add_argument(
        "--browser-user-agent",
        default=os.environ.get("HH_BROWSER_USER_AGENT", DEFAULT_BROWSER_USER_AGENT),
        help=(
            "Значение заголовка User-Agent для HTTP-клиента "
            "(можно задать через HH_BROWSER_USER_AGENT)"
        ),
    )

    parser.add_argument(
        "--proxy",
        default=os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"),
        help="HTTP/HTTPS proxy, например http://user:pass@host:port",
    )

    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Таймаут одного HTTP-запроса в секундах (%(default)s)",
    )

    parser.add_argument(
        "--page-delay-min",
        type=float,
        default=DEFAULT_PAGE_DELAY_MIN,
        help="Минимальная пауза между страницами поиска в секундах (%(default)s)",
    )

    parser.add_argument(
        "--page-delay-max",
        type=float,
        default=DEFAULT_PAGE_DELAY_MAX,
        help="Максимальная пауза между страницами поиска в секундах (%(default)s)",
    )

    parser.add_argument(
        "--vacancy-delay-min",
        type=float,
        default=DEFAULT_VACANCY_DELAY_MIN,
        help="Минимальная пауза между запросами деталей вакансии в секундах (%(default)s)",
    )

    parser.add_argument(
        "--vacancy-delay-max",
        type=float,
        default=DEFAULT_VACANCY_DELAY_MAX,
        help="Максимальная пауза между запросами деталей вакансии в секундах (%(default)s)",
    )

    parser.add_argument(
        "--source",
        choices=["auto", "api", "html"],
        default=DEFAULT_DATA_SOURCE,
        help=(
            "Источник вакансий: auto пытается API и переключается на HTML при блокировке, "
            "api использует только API, html использует только HTML-страницы (%(default)s)"
        ),
    )

    parser.add_argument(
        "--html-description-fallback",
        action="store_true",
        default=os.environ.get("HH_HTML_DESCRIPTION_FALLBACK", "").lower() in {"1", "true", "yes"},
        help=(
            "Если vacancy загружается через HTML и выбран режим key-skills, "
            "автоматически переключаться на description для этой вакансии"
        ),
    )

    parser.add_argument(
        "--no-chart",
        action="store_true",
        default=os.environ.get("HH_NO_CHART", "").lower() in {"1", "true", "yes"},
        help="Не строить PNG-график, только сохранить CSV-результат",
    )

    parser.add_argument(
        "--env-file",
        default=os.environ.get("HH_ENV_FILE", ".env"),
        help="Путь к .env-файлу с переменными окружения (%(default)s)",
    )

    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Не загружать .env-файл перед разбором остальных параметров",
    )

    settings = parser.parse_args(argv)

    logger.debug(f"CLI args: {settings}")
    return settings


@retry_request(max_retries=3, base_delay=1.0, max_delay=30.0)
def fetch_data(url: str, params: dict = None) -> dict:
    """
    Выполняет HTTP GET запрос к указанному URL с параметрами.

    Использует декоратор @retry_request для автоматических повторных попыток
    при ошибках HTTP 403 (Forbidden), 429 (Too Many Requests) и 5xx (серверные ошибки).

    Args:
        url: URL для запроса.
        params: Словарь параметров запроса (query parameters). По умолчанию пустой.

    Returns:
        Словарь с данными ответа (JSON).

    Raises:
        requests.exceptions.RequestException: При сетевых ошибках или HTTP ошибках.
    """
    logger.debug(f"enter fetch_data({locals()})")
    if params is None:
        params = {}

    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    return data


def get_skills_from_description(data: dict) -> list:
    """
    Извлекает навыки из HTML описания вакансии.

    Args:
        data: Словарь с данными вакансии от API HH.

    Returns:
        Список найденных навыков (строки).

    Raises:
        Exception: Если белый список навыков не загружен.
    """
    logger.debug("enter get_skills_from_description(can't show to much data)")

    skill_whitelist = load_skills_whitelist()
    if not skill_whitelist:
        raise Exception('CRITICAL_ERROR: Нет данных в файле "skills_whitelist.txt"')

    desc_html = data.get("description", "")
    soup = BeautifulSoup(desc_html, "html.parser")
    text = soup.get_text()
    skills = extract_skills(text, skill_whitelist)
    return skills


def get_skills_from_key_skills(data: dict) -> list:
    """
    Извлекает навыки из поля key_skills вакансии.

    Args:
        data: Словарь с данными вакансии от API HH.

    Returns:
        Список названий навыков (строки).
    """
    logger.debug("enter get_skills_from_key_skills(can't show to much data)")

    key_skills = data.get("key_skills") or []
    skills = [item["name"] for item in key_skills]
    return skills


def save_result_csv(sorted_skills, file_path="top_skills_all_data.csv"):
    with open(file_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Count", "Skill"])
        for skill, count in sorted_skills.items():
            writer.writerow([int(count), skill])

    if not sorted_skills:
        logger.warning(f"Нет данных для CSV. Создан пустой файл {file_path}")
        return

    logger.info(f"Весь отсортированный список сохранен в файл {file_path}")


@animate(start="Построение графика")
def save_result_chart(sorted_skills, skills_show_count, file_path):
    if pyplot is None:
        logger.warning(
            "matplotlib не установлен. Пропускаю построение графика. "
            "Для полного отключения графика используйте --no-chart."
        )
        return

    if not sorted_skills:
        logger.warning("Нет данных для построения графика. Пропускаю сохранение изображения.")
        return

    # Ограничим до топ-N для графика
    n = skills_show_count
    top_n = list(sorted_skills.items())[:n]
    counts = [int(count) for _, count in top_n]
    skills = [skill for skill, _ in top_n]

    height_per_skill = 0.45
    fig_height = max(10, len(skills) * height_per_skill)
    fig, ax = pyplot.subplots(figsize=(12, fig_height))

    colors = pyplot.cm.viridis(
        [index / max(len(skills) - 1, 1) for index in range(len(skills))]
    )
    bars = ax.barh(skills, counts, color=colors)

    pyplot.subplots_adjust(left=0.3, right=0.95, top=0.95, bottom=0.05)
    ax.tick_params(axis="y", labelsize=11)
    ax.tick_params(axis="x", labelsize=10)

    ax.invert_yaxis()

    for bar, count in zip(bars, counts):
        ax.text(
            bar.get_width() + max(counts) * 0.01,
            bar.get_y() + bar.get_height() / 2,
            str(count),
            va="center",
            fontsize=9,
            color="gray",
        )

    pyplot.title("Частота упоминаний навыков в вакансиях (HH.ru)", fontsize=16, pad=20)
    pyplot.xlabel("Количество упоминаний", fontsize=14)
    pyplot.ylabel("Навыки", fontsize=14)
    fig.savefig(file_path, dpi=150, bbox_inches="tight")
    pyplot.close(fig)

    logger.info(f"График сохранён как '{file_path}'")


def main():
    # Logging
    log_level = os.environ.get("LOGLEVEL", "WARNING").upper()
    logging.basicConfig(
        level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s"
    )
    logger.setLevel(log_level)

    # Настройка параметров (конфигурация)
    bootstrap_settings, remaining_argv = parse_bootstrap_args()
    if not bootstrap_settings.no_dotenv:
        load_dotenv_file(bootstrap_settings.env_file)

    settings = cli_parse(remaining_argv)
    configure_http_session(settings)
    queries = load_queries()

    # Загружаем прогресс
    progress = load_progress()
    if progress.get("queries") == queries and progress.get("mode") == settings.mode:
        processed_ids = set(progress.get("processed_vacancy_ids", []))
        skill_counter = Counter(progress.get("current_skill_counts", {}))
        logger.info("Успешно загружен progress")
    else:
        processed_ids = set()
        skill_counter = Counter()
        logger.info("Progress отсутствует")

    # Парсинг
    if not OPTION_SKIP_PARSING:
        total_to_process = 0
        query_vacancy_map = {}

        logger.info("Начинаю сбор вакансий...")
        # Загрузка ваканский
        for query in queries:
            try:
                vacancies = get_vacancies(
                    query,
                    area=settings.area,
                    vacancies_limit=settings.vacancies_limit,
                    source=settings.source,
                )
            except ProxyUnavailableError as e:
                logger.critical(str(e))
                logger.critical(
                    "Останавливаю весь сбор, потому что без рабочего прокси дальнейшие запросы бессмысленны."
                )
                return
            except BadUserAgentError as e:
                logger.critical(str(e))
                logger.critical(
                    "Останавливаю весь сбор, потому что HH не принимает текущий HH-User-Agent."
                )
                return
            except SourceBlockedError as e:
                logger.critical(str(e))
                logger.critical(
                    "Останавливаю весь сбор, потому что выбранный источник данных оказался заблокирован."
                )
                return
            query_vacancy_map[query] = vacancies
            total_to_process += len(vacancies)
            logger.info(f"Загружено вакансий по запросу '{query}': {len(vacancies)}")

        logger.info(f"Всего вакансий для обработки: {total_to_process}")
        logger.info("Начало обработки вакансий")

        # Счётчик для буферизации записи прогресса
        processed_since_last_save = 0
        
        for query, vacancies in query_vacancy_map.items():
            logger.info(f"Обработка по запросу '{query}'...")
            # Обработка/анализ вакансий
            for v in vacancies:
                id = v["id"]
                name = v["name"]

                logger.info(f'\tОбработка вакансии "{name}"')

                # Проверка на дублирование элемента
                if id in processed_ids:
                    logger.info("\tПропуск вакансии. Была ранее обработана")
                    continue

                # Фильтр
                split_query = re.split("\\s|-", query)
                # Мягкий отсев - должно совпасть хотя бы одно слово
                regex_query = f"({'|'.join(split_query)})"
                if not re.search(regex_query, name, re.I):
                    logger.info("\tОтсев этой вакансии")
                    continue

                # Получение скилов
                try:
                    data = fetch_vacancy_data(v, source=settings.source)
                    effective_mode = resolve_processing_mode(settings, data)
                    if effective_mode != settings.mode:
                        logger.info(
                            '\tHTML fallback: переключаю режим с "%s" на "%s" для вакансии "%s"',
                            settings.mode,
                            effective_mode,
                            name,
                        )

                    match effective_mode:
                        case "description":
                            skills = get_skills_from_description(data)
                        case "key-skills":
                            skills = get_skills_from_key_skills(data)
                        case _:
                            logger.critical("CLI --mode -> Отсутствует handler.")
                            raise Exception("CLI --mode -> Отсутствует handler.")
                            return

                    # Обновляем счётчики
                    for skill in skills:
                        skill_counter[skill] += 1

                    processed_ids.add(id)
                    processed_since_last_save += 1
                    
                    # Сохраняем прогресс каждые save_every вакансий
                    if processed_since_last_save >= settings.save_every:
                        logger.debug(f"Сохранение прогресса после {processed_since_last_save} вакансий (save_every={settings.save_every})")
                        save_progress(
                            {
                                "queries": queries,
                                "mode": settings.mode,
                                "processed_vacancy_ids": list(processed_ids),
                                "current_skill_counts": dict(skill_counter),
                            }
                        )
                        processed_since_last_save = 0
                    
                    # Задержка между запросами (Не допустить перегрев сервера)
                    time.sleep(random.uniform(VACANCY_DELAY_MIN, VACANCY_DELAY_MAX))

                except requests.exceptions.RequestException as e:
                    # Сетевые ошибки или ошибки HTTP
                    logger.error(
                        f"Сетевая ошибка при обработке вакансии {id} ({name}): {type(e).__name__}: {e}"
                    )
                    if hasattr(e.response, 'status_code'):
                        status = e.response.status_code
                        logger.warning(f"HTTP статус: {status}")
                        if status == 429:
                            delay = random.uniform(5, 10)
                            logger.warning(f"Слишком много запросов. Увеличиваю задержку до {delay:.1f}с.")
                            time.sleep(delay)
                        elif status == 403:
                            delay = random.uniform(5, 15)
                            logger.warning(f"Ошибка доступа 403. Увеличиваю задержку до {delay:.1f}с.")
                            # Логируем тело ответа для диагностики
                            try:
                                body = e.response.text[:500]
                                logger.warning(f"Тело ответа 403: {body}")
                            except:
                                pass
                            if is_ddos_guard_response(e.response):
                                logger.warning(
                                    "Детали вакансии тоже блокируются через ddos-guard. "
                                    "С высокой вероятностью проблема уже не в коде запроса, а в репутации IP/канала."
                                )
                            time.sleep(delay)
                        elif status >= 500:
                            delay = random.uniform(3, 5)
                            logger.warning(f"Ошибка сервера. Короткая пауза {delay:.1f}с.")
                            time.sleep(delay)
                        else:
                            delay = random.uniform(2, 4)
                            logger.debug(f"Клиентская ошибка. Пауза {delay:.1f}с.")
                            time.sleep(delay)
                    else:
                        # Общая сетевая ошибка
                        delay = random.uniform(3, 6)
                        logger.warning(f"Сетевая ошибка. Пауза {delay:.1f}с.")
                        time.sleep(delay)
                    continue
                except Exception as e:
                    # Другие ошибки (парсинг, логика)
                    logger.error(
                        f"Ошибка при обработке вакансии {id} ({name}): {type(e).__name__}: {e}",
                        exc_info=True
                    )
                    time.sleep(random.uniform(2, 5))
                    continue

    # Финализируем сохранение прогресса (если остались несохранённые вакансии)
    if processed_since_last_save > 0:
        logger.debug(f"Финальное сохранение прогресса ({processed_since_last_save} вакансий)")
        save_progress(
            {
                "queries": queries,
                "mode": settings.mode,
                "processed_vacancy_ids": list(processed_ids),
                "current_skill_counts": dict(skill_counter),
            }
        )
    
    logger.info("Вакансии обработаны. ")
    logger.info("Строю результаты...")
    sorted_skills = dict(
        sorted(skill_counter.items(), key=lambda x: x[1], reverse=True)
    )

    save_result_csv(sorted_skills)
    if settings.no_chart:
        logger.info("Построение графика отключено флагом --no-chart")
        return

    save_result_chart(
        sorted_skills,
        skills_show_count=settings.skills_show_count,
        file_path=settings.output,
    )


if __name__ == "__main__":
    main()
