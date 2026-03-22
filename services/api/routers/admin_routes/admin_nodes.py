import asyncio
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from providers.registry import registry
from services.api.admin_auth import require_admin

router = APIRouter(
    prefix="/nodes",
    tags=["admin-nodes"],
    dependencies=[Depends(require_admin)],
)

class NodeOut(BaseModel):
    id: str
    coin: str
    status: str           # synced, syncing, error, offline
    block_height: int
    peers: int
    uptime: str
    version: str
    last_sync: datetime

@router.get("", response_model=List[NodeOut], summary="Статус всех блокчейн-узлов")
async def list_nodes() -> List[NodeOut]:
    """
    Возвращает актуальное состояние всех подключенных нод.
    """
    active_providers = registry.all().values()
    
    # Run all status checks in parallel for maximum performance
    tasks = [p.get_node_status() for p in active_providers]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    nodes = []
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            # Fallback if a provider call itself crashes
            p = list(active_providers)[i]
            nodes.append(NodeOut(
                id=f"{p.symbol.lower()}-unavailable",
                coin=p.symbol,
                status="offline",
                block_height=0,
                peers=0,
                uptime="0d 0h 0m",
                version="unknown",
                last_sync=datetime.utcnow()
            ))
        else:
            # res is already a providers.base.NodeStatus object
            nodes.append(NodeOut(
                id=res.id,
                coin=res.coin,
                status=res.status,
                block_height=res.block_height,
                peers=res.peers,
                uptime=res.uptime,
                version=res.version,
                last_sync=res.last_sync
            ))
    
    return nodes
