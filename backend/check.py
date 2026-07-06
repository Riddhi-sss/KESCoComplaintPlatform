import pandas as pd

df2 = pd.read_csv(
    "data/supply_clean.csv",
    usecols=["COMPLAINT_TYPE", "supply_subtype_std", "fault_subtype_std", "FAULT_TYPE"],
    nrows=5000
)

print("supply_subtype_std unique values:")
print(df2["supply_subtype_std"].dropna().unique())
print()
print("fault_subtype_std unique values:")
print(df2["fault_subtype_std"].dropna().unique())
print()
print("FAULT_TYPE unique values:")
print(df2["FAULT_TYPE"].dropna().unique())
print()

df_r_full = pd.read_excel("data/KESCO RE-OPEN DATA 19-JUN'26.xlsx", nrows=5)
print("ALL columns in reopened file:")
print(df_r_full.columns.tolist())
print()
print("Sample rows from reopened file:")
print(df_r_full[["COM_TYPE_NAME", "COM_SUB_TYPE_NAME"]].to_string())