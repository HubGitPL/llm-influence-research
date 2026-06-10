# from google.cloud import bigquery
# from pathlib import Path

# # 'gcloud auth application-default login'
# client = bigquery.Client(project="projekt-badawczy-498814")


# def read_query(query_name: str) -> str:
#     script_path: Path = Path(__file__).resolve().parent
#     base_path = script_path / "queries/"
#     file_path = base_path / query_name
#     with open(file_path, "r") as f:
#         return f.read()


# query_1 = "downloads.sql"
# sql_content = read_query(query_1)

# dry_run_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
# dry_run_job = client.query(sql_content, job_config=dry_run_config)
# size_gb = dry_run_job.total_bytes_processed / (1024**3)
# print(f"--> [DRY RUN] Zapytanie '{query_1}' przeskanuje: {size_gb:.4f} GB")

# MAKS_LIMIT_GB = 5.0
# if size_gb > MAKS_LIMIT_GB:
#     print(f"ANULOWANO: Zapytanie przekracza bezpieczny limit {MAKS_LIMIT_GB} GB!")
# else:
#     print("Rozmiar bezpieczny. Uruchamiam zapytanie...")
#     rows = client.query(sql_content).result()
#     for row in rows:
#         print(row.project, row.downloads)