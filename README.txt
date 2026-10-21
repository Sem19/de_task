# Офлайн-стенд. Як запустити

Тільки про запуск середовища. Самі задачі описані окремо.

Усе працює в Docker. Хмарних доступів не треба, на рантаймі мережа не потрібна.
Перша збірка образів мережу використовує, бо тягне base images.

```bash
make build          # зібрати образи, перший раз кілька хвилин
make help           # усі таргети з описом
```

## Задача 1. Партнерське API

```bash
make api-up         # mock-api на :8080, Postgres на :5432
curl -s localhost:8080/healthz
make extract        # запускає task1_api/extractor/main.py
make psql           # подивитись, що ви завантажили
```

Автоматичний запуск кожні 15 хвилин — окремий сервіс `extractor-scheduler`
(Compose profile `schedule`). Він не стартує з `make api-up` / `make extract`.
`EXTRACT_START_DATE` не задається: після першого прогону йдуть watermark'и.

```bash
make schedule-up    # mock-api + warehouse + supercronic */15 * * * *
make schedule-logs  # stdout/stderr контейнера
make schedule-down  # зупиняє лише планувальник
```

Один цикл: той самий `python main.py`. Перетин прогонів блокує advisory lock
і `flock` у `schedule-run.sh`. `docker compose --profile schedule down` також
зупиняє планувальник.

Креденшели пісочниці лежать у `docker-compose.yml`. Вони несправжні.
Читати `task1_api/mock_api/` можна, тільки напишіть про це у `FINDINGS.md`.

## Задача 2. Склад запитів

```bash
make estate-stats   # перевіряє, що файли складу читаються
make shell          # python 3 + sqlglot + networkx + pandas + duckdb + jq
make compile        # компілює task2_estate/dataform, має вийти з кодом 0
```

**Складу даних для задачі 2 немає.** `raw.*` не існує ніде, куди можна надіслати запит.
Жоден зі 129 запитів запустити не вийде, і `dataform run` не спрацює. Працює тільки
`dataform compile`. Колонки й типи вихідних таблиць описані лише в `schemas.json`.

Це обмеження вправи, а не недогляд. Що з цим робити, написано в завданні, у частині про
`PLAN.md` і секцію `Equivalence`.

## Що є в образі лаби

| Інструмент | Навіщо |
|---|---|
| `dataform` CLI 3.x | єдиний спосіб перевірити ваші actions без складу даних |
| `sqlglot` | нормально парсити BigQuery SQL, знадобиться для графа залежностей |
| `networkx` | алгоритми на графах, якщо захочете |
| `pandas`, `duckdb` | запитувати CSV як таблиці |
| `jq` | для `transfer_configs.json` |

Нічого з цього не обовʼязкове, користуйтесь своїм інструментарієм, якщо так зручніше.
Фіксовані лише дві речі: `make compile` має виходити з кодом 0, а ваш інструментарій
має лежати в репозиторії.
