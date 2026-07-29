"""
Модуль-оркестратор для сбора вакансий по всей территории РФ.

Обходит ограничение API HH.ru в 2000 вакансий на запрос за счет
последовательного запуска парсера по всем Федеральным округам (и крупным городам),
с последующей агрегацией CSV-файлов.

Требования: Наличие основного скрипта parse_skills.py из репозитория hh-skill-parser.
"""

import subprocess
import csv
import sys
from collections import Counter
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE = Path(__file__).resolve().with_name("start.log")

# INFO-лог оркестратора: ограничен 1 MiB и двумя архивами, запись открывается лениво.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=1_024 * 1_024,
            backupCount=2,
            encoding="utf-8",
            delay=True,
        ),
    ],
)
logger = logging.getLogger(__name__)

# ID ключевых регионов и федеральных округов в API HH.ru.
# Полный справочник доступен по ссылке: https://api.hh.ru/areas
REGIONS = {
    "Москва": 1,
    "Санкт-Петербург": 2,
    "Центральный ФО (без Мск)": 13,
    "Северо-Западный ФО (без СПб)": 7,
    "Южный ФО": 10,
    "Северо-Кавказский ФО": 5,
    "Приволжский ФО": 9,
    "Уральский ФО": 4,
    "Сибирский ФО": 6,
    "Дальневосточный ФО": 8,
}

def clean_previous_results() -> None:
    """Удаляет результаты и прогресс предыдущего полного запуска."""
    generated_files = [Path("top_skills_rf.csv")]
    for area_id in REGIONS.values():
        generated_files.extend(
            [Path(f"skills_{area_id}.csv"), Path(f"progress_{area_id}.json")]
        )

    for path in generated_files:
        path.unlink(missing_ok=True)
    logger.info("Удалены результаты и progress предыдущего запуска")


def run_parser_for_area(area_id: int, area_name: str, output_csv: str) -> bool:
    """
    Запускает парсер hh-skill-parser для конкретного региона.
    
    Args:
        area_id (int): ID региона в API HH.ru.
        area_name (str): Название региона (для логов).
        output_csv (str): Путь, куда сохранить итоговый CSV для этого региона.
    """
    logger.info(f"Старт парсинга региона: {area_name} (ID: {area_id})")
    command = [
        sys.executable, "parse_skills.py",
        f"--area={area_id}",
        "--mode=description",
        "--source=html",
        "--html-description-fallback",
        "--no-chart",
        f"--csv-output={output_csv}",
        f"--progress-file=progress_{area_id}.json",
    ]
    
    try:
        # Запускаем основной процесс парсинга
        subprocess.run(command, check=True)
        
        if Path(output_csv).exists():
            logger.info(f"Данные по региону {area_name} сохранены в {output_csv}")
            return True
        logger.error("Парсер завершился без CSV для региона %s", area_name)
        return False
        
    except subprocess.CalledProcessError as e:
        logger.error(f"Ошибка при парсинге региона {area_name}: {e}")
        return False

def aggregate_results(csv_files: list[str]) -> Counter:
    """
    Агрегирует (суммирует) навыки из нескольких CSV-файлов.
    
    Args:
        csv_files (list[str]): Список путей к CSV-файлам с результатами по регионам.
        
    Returns:
        Counter: Общий подсчет упоминаний навыков по всей стране.
    """
    total_skills = Counter()
    
    for file_path in csv_files:
        path = Path(file_path)
        if not path.exists():
            continue
            
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # В твоем коде колонки называются "Count" и "Skill"
                if "Skill" in row and "Count" in row:
                    try:
                        skill = row["Skill"].strip().lower()
                        count = int(row["Count"])
                        total_skills[skill] += count
                    except ValueError:
                        continue
                        
    return total_skills

def run_collection(resume: bool = False) -> None:
    """Собирает вакансии с нуля или продолжает незавершённый запуск."""
    if resume:
        logger.info("Продолжаю незавершённый обход регионов РФ...")
    else:
        clean_previous_results()
        logger.info("Начат новый обход регионов РФ для сбора мобилизационных вакансий...")

    saved_files = []

    # Проходим по всем федеральным округам, собирая данные кусками
    for name, area_id in REGIONS.items():
        region_csv = f"skills_{area_id}.csv"
        if resume and Path(region_csv).exists():
            logger.info("Регион %s уже завершён; пропускаю", name)
            saved_files.append(region_csv)
            continue

        if not run_parser_for_area(area_id, name, region_csv):
            logger.critical(
                "Итог по РФ не построен: сбор остановлен на регионе %s. "
                "Исправьте причину и запустите resume.py для продолжения.",
                name,
            )
            raise SystemExit(2)
        saved_files.append(region_csv)
        
    logger.info("Сбор по регионам завершен. Начинается агрегация статистики...")
    final_stats = aggregate_results(saved_files)
    with open("top_skills_rf.csv", "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Count", "Skill"])
        writer.writerows((count, skill) for skill, count in final_stats.most_common())
    logger.info("Итог по РФ сохранён в top_skills_rf.csv")
    
    # Выводим итоговый топ навыков по всей стране
    print("\n" + "="*60)
    print("ТОП-20 НАВЫКОВ ПО МОБИЛИЗАЦИОННЫМ ВАКАНСИЯМ В РФ")
    print("="*60)
    for skill, count in final_stats.most_common(20):
        print(f"{count:4d} | {skill}")


if __name__ == "__main__":
    run_collection()
