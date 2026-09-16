import pandas as pd
import numpy as np
import unicodedata
import os

# Load data
df1 = pd.read_csv('data/op_data/op.csv', encoding='cp932')
df2 = pd.read_csv('data/op_data/op_full_data.csv', encoding='cp932')

# Combine and drop duplicates
df = pd.concat([df1, df2], ignore_index=True).drop_duplicates()

# Month column
df['年月'] = pd.to_datetime(df['手術実施日']).dt.strftime('%Y-%m')

# Normalize 実施手術室
df['実施手術室'] = df['実施手術室'].fillna('').apply(lambda x: unicodedata.normalize('NFKC', str(x)).upper())

# Flags
df['全身麻酔'] = df['麻酔種別'].fillna('').str.contains('全身麻酔')
df['ダヴィンチSP'] = df['実施手術室'].str.contains('OP-2')
df['ダヴィンチXi'] = df['実施手術室'].str.contains('OP-9')
df['ロボット支援手術'] = df['ダヴィンチSP'] | df['ダヴィンチXi']

# Aggregate by 実施診療科 and 年月
agg_df = df.groupby(['実施診療科', '年月'])[['全身麻酔', 'ロボット支援手術', 'ダヴィンチSP', 'ダヴィンチXi']].sum().reset_index()

# Filter departments with > 0 surgeries
valid_depts = agg_df.groupby('実施診療科')[['全身麻酔', 'ロボット支援手術']].sum().sum(axis=1)
valid_depts = valid_depts[valid_depts > 0].index
agg_df = agg_df[agg_df['実施診療科'].isin(valid_depts)]

output_dir = 'output'
os.makedirs(output_dir, exist_ok=True)

# Generate CSVs (using utf-8-sig so Excel can open it on Windows/Mac properly)
def write_csv(pivot_df, filename):
    pivot_df.index.name = '診療科'
    out_path = os.path.join(output_dir, filename)
    pivot_df.to_csv(out_path, encoding='utf-8-sig')

# Pivot for 全身麻酔
pivot_ga = agg_df.pivot(index='実施診療科', columns='年月', values='全身麻酔').fillna(0).astype(int)
write_csv(pivot_ga, "surgery_monthly_ga.csv")

# Pivot for ロボット支援手術
pivot_robot = agg_df.pivot(index='実施診療科', columns='年月', values='ロボット支援手術').fillna(0).astype(int)
write_csv(pivot_robot, "surgery_monthly_robot_total.csv")

# Pivot for ダヴィンチSP
pivot_sp = agg_df.pivot(index='実施診療科', columns='年月', values='ダヴィンチSP').fillna(0).astype(int)
write_csv(pivot_sp, "surgery_monthly_robot_sp.csv")

# Pivot for ダヴィンチXi
pivot_xi = agg_df.pivot(index='実施診療科', columns='年月', values='ダヴィンチXi').fillna(0).astype(int)
write_csv(pivot_xi, "surgery_monthly_robot_xi.csv")

print("CSVs generated")
