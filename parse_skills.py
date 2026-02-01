import pandas as pd
import requests
import time
import random
from collections import Counter
import matplotlib.pyplot as plt
import seaborn as sns
from bs4 import BeautifulSoup

def get_vacancies(query, area=1, pages=5):
    base_url = 'https://api.hh.ru/vacancies'
    params = {
        'text': query,
        'area': area,
        'per_page': 100,
        'page': 0
    }

    all_vacancies = []
    for page_num in range(pages):
        params['page'] = page_num
        try:
            response = requests.get(base_url, params=params)
            response.raise_for_status()
            data = response.json()
            items = data.get('items', [])
            if not items:
                break
            all_vacancies.extend(items)
            print(f"Обработана страница {page_num + 1} по запросу '{query}'")
            time.sleep(random.uniform(0.25, 0.5))
        except requests.exceptions.RequestException as e:
            print(f"Ошибка при запросе: {e}")
            continue
    return all_vacancies

def load_skills_whitelist(path="skills_whitelist.txt"):
    try:
        with open(path, encoding='utf-8') as f:
            skills = [line.strip().lower() for line in f if line.strip()]
        return set(skills)
    except FileNotFoundError:
        # Если файла нет — используем дефолтный список
        default_skills = {
            "python", "sql", "postgresql", "mysql", "git", "docker",
            "kubernetes", "tensorflow", "pytorch", "scikit-learn",
            "pandas", "numpy", "opencv", "nltk", "spacy", "bert",
            "transformers", "faiss", "elasticsearch", "airflow",
            "spark", "hadoop", "aws", "azure", "gcp", "linux", "bash",
            "c++", "java", "javascript", "react", "vue", "flask",
            "django", "fastapi", "ml", "dl", "ai", "computer vision",
            "natural language processing", "data analysis", "etl",
            "pipeline", "jupyter", "notebook", "latex", "graphql",
            "rest", "api", "json", "yaml", "protobuf", "onnx",
            "triton", "llm", "rag", "prompt engineering"
        }
        return default_skills

SKILL_WHITELIST = load_skills_whitelist()

def extract_technical_skills(text):
    text_lower = text.lower()
    found_skills = []

    for skill in sorted(SKILL_WHITELIST, key=len, reverse=True):
        if skill in text_lower:
            found_skills.append(skill)
            text_lower = text_lower.replace(skill, " ")
    return found_skills

def main():
    queries = ["DataScience", "Machine Learning", "ML Engineer", "Data Scientist", "AI Specialist"]
    all_skills = []
    skill_counter = Counter()

    print("Начинаю сбор вакансий...")
    for q in queries:
        vacancies = get_vacancies(q, pages=3)
        print(f"Найдено вакансий по '{q}': {len(vacancies)}")
        for v in vacancies:
            url = f'https://api.hh.ru/vacancies/{v["id"]}'
            try:
                response = requests.get(url)
                response.raise_for_status()
                data = response.json()
                desc_html = data.get('description', '')
                soup = BeautifulSoup(desc_html, 'html.parser')
                text = soup.get_text()
                tech_skills = extract_technical_skills(text)
                all_skills.extend(tech_skills)
            except Exception as e:
                print(f"Ошибка при извлечении навыков для вакансии {v['id']}: {e}")
                continue

    # Подсчёт
    for skill in all_skills:
        skill_counter[skill] += 1

    # Сортировка
    sorted_skills = dict(sorted(skill_counter.items(), key=lambda x: x[1], reverse=True))

    # Ограничим до топ-20 для графика
    top_20 = dict(list(sorted_skills.items())[:20])

    print("\nТоп-20 навыков:")
    for skill, count in top_20.items():
        print(f"{skill}: {count}")

    # График
    # plt.figure(figsize=(12, 8))
    # sns.barplot(x=list(top_20.values()), y=list(top_20.keys()), palette="viridis")
    # plt.title("Частота упоминаний навыков в вакансиях (HH.ru)", fontsize=16)
    # plt.xlabel("Количество упоминаний", fontsize=14)
    # plt.ylabel("Навыки", fontsize=14)
    # plt.tight_layout()
    # plt.savefig("hh_skills_bar_chart.png", dpi=150)
    # print("\nГрафик сохранён как 'hh_skills_bar_chart.png'")

    # Построение графика
    df = pd.DataFrame(list(top_20.items()), columns=['Count', 'Skill'])

    plt.figure(figsize=(12, 8))
    sns.barplot(data=df, x='Count', y='Skill', palette="viridis", legend=False)
    plt.title("Частота упоминаний навыков в вакансиях (HH.ru)", fontsize=16)
    plt.xlabel("Количество упоминаний", fontsize=14)
    plt.ylabel("Навыки", fontsize=14)
    plt.tight_layout()
    plt.savefig("hh_skills_bar_chart.png", dpi=150)
    print("\nГрафик сохранён как 'hh_skills_bar_chart.png'")

if __name__ == "__main__":
    main()