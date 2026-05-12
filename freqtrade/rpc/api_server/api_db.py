import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Body, HTTPException
from freqtrade.rpc.api_server.deps import get_config

logger = logging.getLogger(__name__)

router = APIRouter()

def get_db_path(config: Dict[str, Any], db_name: str) -> Path:
    user_data_path = Path(config.get('user_data_dir', 'user_data'))
    db_path = (user_data_path / db_name).resolve()
    
    # Security check: ensure the path is inside user_data_dir
    if not db_path.is_relative_to(user_data_path.resolve()):
        raise HTTPException(status_code=403, detail="Access denied to this path")
    
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database {db_name} not found")
        
    return db_path

@router.get("/db_list", tags=["Database"])
def list_databases(config=Depends(get_config)):
    """List all sqlite databases in user_data directory"""
    user_data_path = Path(config.get('user_data_dir', 'user_data'))
    dbs = []
    for ext in ['*.sqlite', '*.db']:
        for f in user_data_path.glob(ext):
            dbs.append(f.name)
    return sorted(dbs)

@router.post("/db_query", tags=["Database"])
def execute_query(
    config=Depends(get_config),
    db: str = Body(..., embed=True),
    sql: str = Body(..., embed=True)
):
    """Execute a raw SQL query and return results"""
    db_path = get_db_path(config, db)
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute(sql)
        
        if cursor.description:
            columns = [column[0] for column in cursor.description]
            rows = [dict(row) for row in cursor.fetchall()]
            return {
                "columns": columns,
                "rows": rows,
                "count": len(rows),
                "error": None
            }
        else:
            conn.commit()
            return {
                "columns": [],
                "rows": [],
                "count": cursor.rowcount,
                "error": None,
                "message": f"Query executed successfully. Affected rows: {cursor.rowcount}"
            }
            
    except sqlite3.Error as e:
        return {
            "columns": [],
            "rows": [],
            "count": 0,
            "error": str(e)
        }
    finally:
        if conn:
            conn.close()

@router.post("/db_tables", tags=["Database"])
def list_tables(
    config=Depends(get_config),
    db: str = Body(..., embed=True)
):
    """List all tables in the selected database"""
    db_path = get_db_path(config, db)
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        
        # Query for all table names in sqlite_master
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = [row[0] for row in cursor.fetchall()]
        
        return sorted(tables)
            
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()
