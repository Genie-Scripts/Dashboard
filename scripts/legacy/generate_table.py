import pandas as pd
import numpy as np

# Load data
df1 = pd.read_csv('data/op_data/op.csv', encoding='cp932')
df2 = pd.read_csv('data/op_data/op_full_data.csv', encoding='cp932')

# Combine and drop duplicates (assuming exact duplicates)
df = pd.concat([df1, df2], ignore_index=True).drop_duplicates()

# Month column
df['年月'] = pd.to_datetime(df['手術実施日']).dt.strftime('%Y-%m')

# Normalize 実施手術室 more robustly
# Convert to string, normalize full-width to half-width for alphanumeric and symbols
import unicodedata
df['実施手術室'] = df['実施手術室'].fillna('').apply(lambda x: unicodedata.normalize('NFKC', str(x)).upper())

# Flags
df['全身麻酔'] = df['麻酔種別'].fillna('').str.contains('全身麻酔')
df['ダヴィンチSP'] = df['実施手術室'].str.contains('OP-2')
df['ダヴィンチXi'] = df['実施手術室'].str.contains('OP-9')
df['ロボット支援手術'] = df['ダヴィンチSP'] | df['ダヴィンチXi']

# Aggregate by 実施診療科 and 年月
agg_df = df.groupby(['実施診療科', '年月'])[['全身麻酔', 'ロボット支援手術', 'ダヴィンチSP', 'ダヴィンチXi']].sum().reset_index()

# We only care about departments that have at least one relevant surgery to keep the table clean
valid_depts = agg_df.groupby('実施診療科')[['全身麻酔', 'ロボット支援手術']].sum().sum(axis=1)
valid_depts = valid_depts[valid_depts > 0].index
agg_df = agg_df[agg_df['実施診療科'].isin(valid_depts)]

artifact_path = '/Users/genie/.gemini/antigravity-cli/brain/14725ba3-a9de-4ade-b995-29267563e850/surgery_analysis_results.md'

with open(artifact_path, 'w') as f:
    f.write("# 診療科別 手術件数 月次推移\n\n")
    f.write("提供された2つのCSVファイル（重複排除済み）をもとに、全身麻酔手術およびロボット支援手術の件数を月次で集計しました。\n\n")

    def write_markdown(pivot_df, title):
        f.write(f"## {title}\n")
        pivot_df.index.name = None
        cols = pivot_df.columns.tolist()
        header = "| 診療科 | " + " | ".join(cols) + " |"
        separator = "|---|" + "|".join(["---"] * len(cols)) + "|"
        f.write(header + "\n")
        f.write(separator + "\n")
        for index, row in pivot_df.iterrows():
            row_str = f"| {index} | " + " | ".join(str(int(val)) for val in row) + " |"
            f.write(row_str + "\n")
        f.write("\n")

    # Pivot for 全身麻酔
    pivot_ga = agg_df.pivot(index='実施診療科', columns='年月', values='全身麻酔').fillna(0).astype(int)
    write_markdown(pivot_ga, "全身麻酔手術")

    # Pivot for ロボット支援手術
    pivot_robot = agg_df.pivot(index='実施診療科', columns='年月', values='ロボット支援手術').fillna(0).astype(int)
    write_markdown(pivot_robot, "ロボット支援手術 (合計)")

    # Pivot for ダヴィンチSP
    pivot_sp = agg_df.pivot(index='実施診療科', columns='年月', values='ダヴィンチSP').fillna(0).astype(int)
    write_markdown(pivot_sp, "ダヴィンチSP (OP-2)")

    # Pivot for ダヴィンチXi
    pivot_xi = agg_df.pivot(index='実施診療科', columns='年月', values='ダヴィンチXi').fillna(0).astype(int)
    write_markdown(pivot_xi, "ダヴィンチXi (OP-9)")

print("Artifact written with fixed normalization")
