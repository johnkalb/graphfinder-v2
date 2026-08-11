import os
from datetime import datetime, timedelta

try:
    import db
except ImportError:
    from webapp import db

def check_performance(db_path, threshold_ms=500):
    if not db.IS_POSTGRES and not os.path.exists(db_path):
        return []
    conn = db.connect(db_path)
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    # Find routes where average latency > threshold in last 24h
    alerts = conn.execute("""
        SELECT route, AVG(latency_ms) as avg_lat, COUNT(*) as req_count
        FROM request_logs 
        WHERE timestamp > ? 
        GROUP BY route 
        HAVING avg_lat > ?
        ORDER BY avg_lat DESC
    """, (yesterday, threshold_ms)).fetchall()
    conn.close()
    return alerts

if __name__ == "__main__":
    db_path = os.path.join(os.path.dirname(__file__), "data", "ops_metrics.db")
    alerts = check_performance(db_path)
    if alerts:
        print(f"Performance Alerts (>{500}ms average latency in last 24h):")
        for a in alerts:
            print(f"  {a['route']}: {a['avg_lat']:.2f}ms (count: {a['req_count']})")
    else:
        print("No performance alerts found.")
