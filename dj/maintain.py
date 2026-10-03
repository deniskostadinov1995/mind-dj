"""Maintenance that has to happen in the right order.

Clustering and learned preferences are coupled: rebuilding the vibes renumbers
them, which orphans every preference learned against the old numbering. These
two steps must always run together, so they live behind one function rather
than relying on whoever calls them to remember.
"""
from . import embed, taste
from .config import load


def recluster(conn, cfg=None, k=None):
    """Rebuild vibe clusters and replay history into the context model."""
    cfg = cfg or load()
    k = k or cfg["taste"]["vibe_count"]
    vibes = embed.rebuild_vibes(conn, k=k)
    plays = taste.rebuild_ctx_pref(conn, cfg)
    return {"vibes": vibes, "plays_replayed": plays}


def refresh(conn, cfg=None, progress=None):
    """Embed anything new, then recluster. Safe to run at any time."""
    cfg = cfg or load()
    emb = embed.Embedder(cfg)
    done, total = emb.backfill(conn, progress=progress)
    out = {"embedded": done, "pending": total}
    if done or total == 0:
        out.update(recluster(conn, cfg))
    return out
