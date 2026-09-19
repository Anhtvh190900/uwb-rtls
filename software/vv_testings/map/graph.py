import numpy as np
from scipy.spatial import cKDTree
import networkx as nx

class Grapher:
    def __init__(self, path: str = "src/utils/path_finding/assets/Competition_track_graph.graphml.xml", scale: float = 1.0, shift: np.ndarray = np.array([0, 0]), eps: float = .5):
        G = nx.read_graphml(path)
        print(f"\nGraph Summary:\n"
              f"- Nodes:    {G.number_of_nodes()}\n"
              f"- Edges:    {G.number_of_edges()}\n"
              f"- Directed: {G.is_directed()}\n")

        # ==================== STRUCTURED NODES ===================
        ys = [float(d.get('y',0.0)) * scale for _, d in G.nodes(data=True)]
        yMin, yMax = min(ys), max(ys)
        self.nodeMap = {}; self.nodeMapOrig = {}
        for node_id, data in G.nodes(data=True):
            nid = int(node_id)
            x = float(data.get('x', 0.0)) * scale - shift[0]
            y = float(data.get('y', 0.0)) * scale
            yOrig = y
            y = (yMax - (y - yMin)) - shift[1]
            self.nodeMap[nid] = (x, y)
            self.nodeMapOrig[nid] = (x, yOrig)

        self.yMin = yMin; self.yMax = yMax

        self.nodeIds = sorted(self.nodeMap.keys())

        # ==================== STRUCTURED EDGE =====================
        edge_list = []
        for u, v, data in G.edges(data=True):
            edge_list.append((int(u), int(v), bool(data['dotted'])))
        self.edgeRecords = np.array(
            edge_list,
            dtype=[('u','i4'), ('v','i4'), ('dotted','?')]
        )
        
        self.groupDup = self.duplicateGroupFind(eps)
        self._dupMap = {}
        for cluster in self.groupDup:
            for nid in cluster:
                self._dupMap[nid] = cluster

    def __getitem__(self, key):
        """
        g['node']           → array of all node coords in ID order
        g['node', i]        → (x,y) of node with ID == i
        g['node', start:stop:step']
                            → list of coords for those IDs
        
        Same for 'edge', returning tuples (u, v, dotted_flag).
        """
        if isinstance(key, tuple) and len(key) == 2:
            kind, idx = key
        else:
            kind, idx = key, slice(None)

        def is_int_sequence(x):
            return (
                isinstance(x, (list, tuple)) or
                (isinstance(x, np.ndarray) and np.issubdtype(x.dtype, np.integer))
            )

        if kind == 'node':
            if isinstance(idx, slice):
                ids = self.nodeIds[idx]
                return np.array([self.nodeMap[i] for i in ids])
            if is_int_sequence(idx):
                return np.array([self.nodeMap[int(i)] for i in idx])
            if isinstance(idx, (int, np.integer)):
                if idx not in self.nodeMap:
                    raise KeyError(f"No such node ID: {idx}")
                return np.array(self.nodeMap[idx])
            raise KeyError(f"Invalid node lookup: {idx!r}")
        
        if kind == 'nodeOrig':
            if isinstance(idx, slice):
                ids = self.nodeIds[idx]
                return np.array([self.nodeMapOrig[i] for i in ids])
            if is_int_sequence(idx):
                return np.array([self.nodeMapOrig[int(i)] for i in idx])
            if isinstance(idx, (int, np.integer)):
                if idx not in self.nodeMapOrig:
                    raise KeyError(f"No such node ID: {idx}")
                return np.array(self.nodeMapOrig[idx])
            raise KeyError(f"Invalid node lookup: {idx!r}")


        elif kind == 'edge':
            if isinstance(idx, slice):
                return self.edgeRecords[idx]
            if is_int_sequence(idx):
                rows = []
                for i in idx:
                    i = int(i)
                    if i in self.nodeIds:
                        mask = (self.edgeRecords['u'] == i)
                        subset = self.edgeRecords[mask]
                        if subset.size:
                            u_c = subset['u']
                            v_c = subset['v']
                            d_c = subset['dotted'].astype(np.int32)
                            rows.append(np.column_stack([u_c, v_c, d_c]))
                    elif 1 <= i <= len(self.edgeRecords):
                        rec = self.edgeRecords[i-1]
                        rows.append(np.array([[rec['u'], rec['v'], int(rec['dotted'])]]))
                    else:
                        raise KeyError(f"No such edge or node ID: {i}")
                if rows:
                    return np.vstack(rows)
                else:
                    return np.empty((0,3), dtype=int)
            if isinstance(idx, (int, np.integer)):
                if idx in self.nodeIds:
                    mask = (self.edgeRecords['u'] == idx)
                    subset = self.edgeRecords[mask]
                    u_c = subset['u']
                    v_c = subset['v']
                    d_c = subset['dotted'].astype(np.int32)
                    return np.column_stack([u_c, v_c, d_c])

                if 1 <= idx <= len(self.edgeRecords):
                    rec = self.edgeRecords[idx-1]
                    return np.array([[rec['u'], rec['v'], int(rec['dotted'])]])

            raise KeyError(f"Invalid edge lookup: {idx!r}")

        else:
            raise KeyError(f"Unknown kind {kind!r}; use 'node' or 'edge' or 'nodeOrig")

    @property
    def max(self):
        arr = np.array(list(self.nodeMap.values()))
        return arr.max(axis=0)

    @property
    def min(self):
        arr = np.array(list(self.nodeMap.values()))
        return arr.min(axis=0)
    
    def nearestNodes(self, point: np.ndarray, numNodes: int = 2 ):
        coords = self['node']                        # (N×2) array
        tree = cKDTree(coords)                        # build KD-tree
        allDists, allIndices = tree.query(point, k=len(coords))
        
        allDists   = np.atleast_1d(allDists)
        allIndices = np.atleast_1d(allIndices)

        seenClusters = set()
        nodeIdList   = []
        distList     = []
        clusterList  = []

        for dist, idx in zip(allDists, allIndices):
            nodeId  = self.nodeIds[idx]
            cluster = tuple(sorted(self._dupMap.get(nodeId, [nodeId])))

            if cluster in seenClusters:
                continue

            seenClusters.add(cluster)
            nodeIdList.append(nodeId)
            distList.append(dist)
            clusterList.extend(cluster)

            if len(nodeIdList) >= numNodes:
                break

        return clusterList
    
    def currentEdge(self, point, numSearchNodes: int = 2):
        clusterList = self.nearestNodes(point, numSearchNodes)
        candidateEdge = self['edge', clusterList]
        mask = np.isin(candidateEdge[:, 1], clusterList)
        candidateEdge = candidateEdge[mask]

        dist2Candidate = []
        for edge in candidateEdge:
            dist = self.pointLineDistanceProj(point, *self['node', edge[:2]])
            edge = self.edgeOppositeTest(point, *self['node', edge[:2]])
            dist2Candidate.append(dist if edge == True else np.inf)

        
        return candidateEdge[np.argmin(dist2Candidate)]    
        
    @staticmethod
    def edgeOppositeTest(pt, A, B):
        P = np.asarray(pt, dtype=float)
        A = np.asarray(A, dtype=float)
        B = np.asarray(B, dtype=float)
        AB = B - A
        denom = np.dot(AB, AB)
        if denom == 0:
            return False   # edge of length zero
        
        t = np.dot(P - A, AB) / denom
        return (0.0 < t) and (t < 1.0)

    @staticmethod
    def pointLineDistanceProj(pt, A, B):
        P = np.array(pt, dtype=float)
        A = np.array(A, dtype=float)
        B = np.array(B, dtype=float)
        AB = B - A
        t  = np.dot(P - A, AB) / np.dot(AB, AB)
        closest = A + t * AB
        return np.linalg.norm(P - closest) 
    
    def duplicateGroupFind(self, epsilon: float):
        coords = self['node']
        ids    = self.nodeIds               
        N      = len(coords)

        tree  = cKDTree(coords)
        pairs = tree.query_pairs(r=epsilon)

        parent = list(range(N))
        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a
        def union(a,b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
        for i,j in pairs:
            union(i, j)

        clusters = {}
        for idx in range(N):
            root = find(idx)
            clusters.setdefault(root, []).append(ids[idx])

        return [grp for grp in clusters.values() if len(grp) > 1]
    
    def savePath(self, path: list[int], fname: str):
        np.savetxt(fname, path, fmt="%d", delimiter=",")

    def loadPath(self, fname: str) -> list[int]:
        return np.loadtxt(fname, dtype=int, delimiter=",").tolist()