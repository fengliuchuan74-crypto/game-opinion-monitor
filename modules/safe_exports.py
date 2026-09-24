"""Publish a complete version only after every workbook succeeds."""
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
import pandas as pd
from .deliverables import excel_bytes
from .operations import analyze_reviews
from .review_store import list_game_profiles, read_reviews


def safe_export_name(value, fallback='game'):
    name=''.join(c if c.isalnum() else '_' for c in str(value or '')).strip('_')
    return (name or fallback)[:80]


def export_reviews_by_game(db_path,output_dir):
    output_dir=Path(output_dir)
    output_dir.mkdir(parents=True,exist_ok=True)
    name=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    destination=output_dir/name
    files=[]
    with tempfile.TemporaryDirectory(prefix='.preparing_',dir=output_dir) as temporary:
        stage=Path(temporary)/'complete'
        stage.mkdir()
        for _,profile in list_game_profiles(db_path).iterrows():
            reviews=read_reviews(db_path,app_id=str(profile['app_id']),country=str(profile['country']))
            if reviews.empty: continue
            filename=f"{safe_export_name(profile['app_name'])}_{profile['app_id']}_{profile['country']}.xlsx"
            (stage/filename).write_bytes(excel_bytes({'comments':analyze_reviews(reviews,db_path),'profile':pd.DataFrame([profile.to_dict()])}))
            files.append(destination/filename)
        os.replace(stage,destination)
    return files
