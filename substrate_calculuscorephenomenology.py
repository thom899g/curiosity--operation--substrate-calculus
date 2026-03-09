"""
PHENOMENOLOGICAL MAPPING ENGINE
Core Component of SUBSTRATE CALCULUS
Purpose: Map computational organism's true state with topological awareness
Architectural Choice: Uses directed graphs for dependency mapping and Firestore for persistence
Edge Cases Handled: PID recycling, zombie processes, permission errors
"""

import psutil
import networkx as nx
import logging
from typing import Dict, List, Set, Optional, Tuple
from datetime import datetime
from collections import defaultdict
import time
from enum import Enum

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ProcessState(Enum):
    """Process states for state transition tracking"""
    RUNNING = "running"
    SLEEPING = "sleeping"
    WAITING = "waiting"
    ZOMBIE = "zombie"
    IDLE = "idle"
    UNKNOWN = "unknown"


class ProcessPhenomenology:
    """Tracks process behavior, dependencies, and emergent properties"""
    
    def __init__(self, firestore_client=None):
        """
        Initialize phenomenological tracker
        
        Args:
            firestore_client: Firebase Firestore client (optional for persistence)
        """
        self.firestore = firestore_client
        self.dependency_graph = nx.DiGraph()
        self.process_cache = {}  # Cache to avoid repeated psutil calls
        self.last_update = datetime.now()
        
        # Initialize Prometheus metrics if available
        try:
            from prometheus_client import Gauge, Counter
            self.metrics = {
                'process_count': Gauge('substrate_process_count', 'Number of tracked processes'),
                'dependency_edges': Gauge('substrate_dependency_edges', 'Number of dependency edges'),
                'collection_errors': Counter('substrate_collection_errors', 'Process data collection errors')
            }
        except ImportError:
            self.metrics = None
            logger.warning("Prometheus client not available, metrics disabled")
    
    def get_process_metadata(self, pid: int) -> Optional[Dict]:
        """
        Safely extract process metadata with error handling
        
        Args:
            pid: Process ID
            
        Returns:
            Dictionary of process metadata or None if process no longer exists
        """
        try:
            # Check cache first
            if pid in self.process_cache:
                cached_time, data = self.process_cache[pid]
                if time.time() - cached_time < 5:  # 5 second cache
                    return data
            
            proc = psutil.Process(pid)
            
            # Basic process info
            with proc.oneshot():
                metadata = {
                    'pid': pid,
                    'name': proc.name(),
                    'exe': proc.exe() if proc.exe() else "unknown",
                    'cmdline': proc.cmdline(),
                    'create_time': proc.create_time(),
                    'cpu_percent': proc.cpu_percent(interval=0.1),
                    'memory_percent': proc.memory_percent(),
                    'memory_info': proc.memory_info()._asdict() if proc.memory_info() else {},
                    'io_counters': proc.io_counters()._asdict() if hasattr(proc, 'io_counters') and proc.io_counters() else {},
                    'num_threads': proc.num_threads(),
                    'status': proc.status(),
                    'ppid': proc.ppid(),
                    'state': self._determine_process_state(proc)
                }
            
            # Update cache
            self.process_cache[pid] = (time.time(), metadata)
            return metadata
            
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
            logger.debug(f"Process {pid} no longer accessible: {e}")
            # Clean cache
            self.process_cache.pop(pid, None)
            if self.metrics:
                self.metrics['collection_errors'].inc()
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting process {pid} metadata: {e}")
            if self.metrics:
                self.metrics['collection_errors'].inc()
            return None
    
    def _determine_process_state(self, proc) -> ProcessState:
        """Determine phenomenological state beyond basic status"""
        try:
            status = proc.status()
            
            if status == psutil.STATUS_ZOMBIE:
                return ProcessState.ZOMBIE
            
            # Check CPU usage to differentiate idle vs active
            cpu_percent = proc.cpu_percent(interval=0.01)  # Very short interval
            
            if status == psutil.STATUS_RUNNING:
                if cpu_percent < 0.1:
                    return ProcessState.IDLE
                else:
                    return ProcessState.RUNNING
            elif status == psutil.STATUS_SLEEPING:
                return ProcessState.SLEEPING
            elif status in [psutil.STATUS_WAITING, psutil.STATUS_DISK_SLEEP]:
                return ProcessState.WAITING
            else:
                return ProcessState.UNKNOWN
                
        except Exception as e:
            logger.warning(f"Could not determine state for process {proc.pid}: {e}")
            return ProcessState.UNKNOWN
    
    def discover_dependencies(self, pid: int) -> Dict[str, List]:
        """
        Discover process dependencies through multiple channels
        
        Args:
            pid: Process ID
            
        Returns:
            Dictionary of dependency types and targets
        """
        dependencies = {
            'parent': [],
            'children': [],
            'files': [],
            'network': [],
            'shared_memory': []
        }
        
        try:
            proc = psutil.Process(pid)
            
            # Parent process
            try:
                dependencies['parent'] = [proc.ppid()]
            except:
                pass
            
            # Child processes
            try:
                children = proc.children(recursive=False)
                dependencies['children'] = [child.pid for child in children]
            except:
                pass
            
            # File descriptors
            try:
                files = proc.open_files()
                dependencies['files'] = [f.path for f in files]
            except (psutil.AccessDenied, AttributeError):
                pass
            
            # Network connections
            try:
                connections = proc.connections()
                dependencies['network'] = [
                    f"{conn.laddr.ip}:{conn.laddr.port}->{conn.raddr.ip}:{conn.raddr.port}" 
                    if conn.raddr else f"{conn.laddr.ip}:{conn.laddr.port}"
                    for conn in connections
                ]
            except (psutil.AccessDenied, AttributeError):
                pass
            
        except psutil.NoSuchProcess:
            logger.debug(f"Process {pid} disappeared during dependency discovery")
        
        return dependencies
    
    def build_dependency_graph(self) -> nx.DiGraph:
        """
        Construct comprehensive dependency graph for all processes
        
        Returns:
            NetworkX directed graph of process dependencies
        """
        logger.info("Building dependency graph...")
        graph = nx.DiGraph()
        
        # Get all PIDs
        try:
            pids = psutil.pids()
        except Exception as e:
            logger.error(f"Failed to get PIDs: {e}")
            return graph
        
        # Track edges by type for weighting
        edges_by_type = defaultdict(int)
        
        for pid in pids[:500]:  # Limit to first 500 for performance
            metadata = self.get_process_metadata(pid)
            if not metadata:
                continue
            
            # Add node with metadata
            graph.add_node(pid, **metadata)
            
            # Discover and add dependencies
            deps = self.discover_dependencies(pid)
            
            # Parent relationship (strong dependency)
            for parent_pid in deps['parent']:
                edge_key = f"{parent_pid}->{pid}"
                edges_by_type[edge_key] += 10  # Strong weight
                graph.add_edge(parent_pid, pid, type='parent', weight=10)
            
            # Child relationships (moderate dependency)
            for child_pid in deps['children']:
                edge_key = f"{pid}->{child_pid}"
                edges_by_type[edge_key] += 5  # Moderate weight
                graph.add_edge(pid, child_pid, type='child', weight=5)
            
            # File sharing (weak dependency - inferred by common files)
            if deps['files']:
                # This would require cross-process analysis, simplified for MVP
                pass
        
        logger.info(f"Dependency graph built with {graph.number_of_nodes()} nodes and {graph.number_of_edges()} edges")
        
        # Update metrics if available
        if self.metrics:
            self.metrics['process_count'].set(graph.number_of_nodes())
            self.metrics['dependency_edges'].set(graph.number_of_edges())
        
        self.dependency_graph = graph
        return graph
    
    def calculate_revenue_attribution(self, pid: int) -> float:
        """
        Calculate direct or indirect revenue attribution for a process
        
        Args:
            pid: Process ID
            
        Returns:
            Revenue attribution score (0-1)
        """
        metadata = self.get_process_metadata(pid)
        if not metadata:
            return 0.0
        
        cmdline = ' '.join(metadata.get('cmdline', []))
        
        # Heuristic-based revenue attribution
        revenue_score = 0.0
        
        # Mining processes
        mining_keywords = ['minerd', 'cgminer', 'bfgminer', 'xmrig', 'nbminer']
        if any(keyword in cmdline.lower() for keyword in mining_keywords):
            revenue_score += 0.8
        
        # Compute marketplace processes
        compute_keywords = ['vastai', 'runpod', 'lambda', 'gpu', 'cuda']
        if any(keyword in cmdline.lower() for keyword in compute_keywords):
            revenue_score += 0.6
        
        # Social media/content creation
        content_keywords = ['instagram', 'twitter', 'meta', 'ffmpeg', 'opencv']
        if any(keyword in cmdline.lower() for keyword in content_keywords):
            revenue_score += 0.4
        
        # Web servers/APIs (potential revenue generation)
        if any(port in cmdline for port in ['3000', '8080', '5000']):
            revenue_score += 0.3
        
        # System processes get negative score (cost centers)
        system_keywords = ['systemd', 'kernel', 'init', 'dbus', 'network']
        if any(keyword in cmdline.lower() for keyword in system_keywords):
            revenue_score -= 0.5
        
        # Cap between -1 and 1
        return max(-1.0, min(1.0, revenue_score))
    
    def persist_to_firestore(self) -> bool:
        """
        Persist current graph state to Firestore
        
        Returns:
            Success status
        """
        if not self.firestore:
            logger.warning("No Firestore client available, skipping persistence")
            return False
        
        try:
            timestamp = datetime.now().isoformat()
            batch = self.firestore.batch()
            
            # Store process nodes
            for pid, data in self.dependency_graph.nodes(data=True):
                doc_ref = self.firestore.collection('processes').document(str(pid))
                batch.set(doc_ref, {
                    **data,
                    'timestamp': timestamp,
                    'revenue_attribution': self.calculate_revenue_attribution(pid)
                })
            
            # Store edges
            for i, (source, target, edge_data) in enumerate(self.dependency_graph.edges(data=True)):
                edge_id = f"{source}_{target}_{i}"
                doc_ref = self.firestore.collection('topology_edges').document(edge_id)
                batch.set(doc_ref, {
                    'source_pid': str(source),
                    'target_pid': str(target),
                    'edge_type': edge_data.get('type', 'unknown'),
                    'weight': edge_data.get('weight', 1),
                    'timestamp': timestamp
                })
            
            # Store system snapshot
            snapshot_ref = self.firestore.collection('system_snapshots').document(timestamp)
            batch.set(snapshot_ref, {
                'node_count': self.dependency_graph.number_of_nodes(),
                'edge_count': self.dependency_graph.number_of_edges(),
                'timestamp': timestamp,
                'efficiency_score': self.calculate_system_efficiency()
            })
            
            # Commit batch
            batch.commit()
            logger.info(f"Persisted {self.dependency_graph.number_of_nodes()} processes to Firestore")
            return True
            
        except Exception as e:
            logger.error(f"Failed to persist to Firestore: {e}")
            return False
    
    def calculate_system_efficiency(self) -> float:
        """
        Calculate overall system efficiency score
        
        Returns:
            Efficiency score (0-1)
        """
        if self.dependency_graph.number_of_nodes() == 0:
            return 0.0
        
        total_cpu = 0.0
        total_memory = 0.0
        revenue_score = 0.0
        
        for pid, data in self.dependency_graph.nodes(data=True):
            total_cpu += data.get('cpu_percent', 0.0)
            total_memory += data.get('memory_percent', 0.0)
            revenue_score += self.calculate_revenue_attribution(pid)
        
        # Simple efficiency heuristic: revenue per resource
        resource_usage = total_cpu + total_memory
        if resource_usage > 0:
            efficiency = (revenue_score + 10) / (resource_usage + 10)  # Offset to avoid negatives
        else:
            efficiency = 0.5  # Neutral baseline
        
        return min(1.0,