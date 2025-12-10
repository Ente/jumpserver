from django.db.models import F
from typing import Optional
from collections import defaultdict

from users.models import User
from assets.models import Asset, Node
from perms.models import AssetPermission

from common.utils import lazyproperty
from orgs.utils import current_org


class TreeNode:

    class Type:
        BRIDGE = 'bridge'
        OWNER = 'owner'
        DA = 'da'

    def __init__(self, key, tp, assets=None):
        self.key = key
        self.type = tp
        # 节点下的直接资产集合，不包含子孙节点的资产
        self._assets = set() if assets is None else set(assets)
        self._assets_amount = 0
    
    def add_assets(self, asset_ids):
        self._assets.update(asset_ids)

    @property
    def assets(self):
        return self._assets
    
    @property
    def assets_amount(self):
        return self._assets_amount

    def assets_amount_increment(self, amount=1):
        self._assets_amount += amount
    
    def can_be_overridden(self, other: 'TreeNode'):
        """
        # 不可以
        owner owner
        owner bridge
        owner da

        # 可以
        da da
        # 可以
        da owner
        # 不可以
        da bridge

        # 可以
        bridge da
        bridge owner
        bridge bridge
        """
        if self.key != other.key:
            return False
        if self.type == self.Type.OWNER:
            return False
        if self.type == self.Type.DA and other.type == self.Type.BRIDGE:
            return False
        return True


class Tree:

    separator = ':'

    def __init__(self, nodes: Optional[list[TreeNode]] = None, org_id=None):
        # {node_key: TreeNode}
        self._nodes = defaultdict(TreeNode)
        self._org_id = org_id
        self.init(nodes)
    
    def init(self, nodes: Optional[list[TreeNode]]):
        if nodes is None:
            return
        for node in nodes:
            self.add_node(node)
        self._reverse_generated()
    
    def _reverse_generated(self):
        """ 逆向生成树 """
        for key in list(self._nodes.keys()):
            ancestor_keys = Node.get_node_ancestor_keys(key)
            for ancestor_key in ancestor_keys:
                # 自动生成的祖先节点默认标记为 bridge, 添加时会判断是否要覆盖已经存在的节点
                tree_node = TreeNode(key=ancestor_key, tp=TreeNode.Type.BRIDGE)
                self.add_node(tree_node)
    
    def merge(self, other: 'Tree') -> 'Tree':
        merged_tree = Tree()
        for node in self._nodes.values():
            merged_tree.add_node(node)
        for node in other._nodes.values():
            merged_tree.add_node(node)
        merged_tree._finalize()
        return merged_tree
    
    def _finalize(self):
        self._prune()
        self._init_owner_nodes_children()
        self._compute_assets_amount()
        self._sorted()
    
    def _sorted(self):
        self._nodes = defaultdict(
            TreeNode, 
            sorted(self._nodes.items(), key=lambda item: [int(i) for i in item[0].split(':')])
        )
    
    def _init_owner_nodes_children(self):
        """ 初始化 Owner-Node 的所有子孙节点以及其下的直接资产 """
        owner_nodes = self._owner_nodes
        if not owner_nodes:
            return
        nodes = Node.get_nodes_all_children(owner_nodes, with_self=True)
        node_id_key_sets = nodes.annotate(char_id=F('id')).values_list('char_id', 'key')
        node_id_key_mapper = dict(node_id_key_sets)

        node_ids = node_id_key_mapper.keys()
        nid_aid_sets = Node.assets.through.objects.filter(node_id__in=node_ids).annotate(
            char_nid=F('node_id'), char_aid=F('asset_id')).values_list('char_nid', 'char_aid')
        
        for nid, aid in nid_aid_sets:
            key = node_id_key_mapper.get(nid)
            if not key:
                continue
            tree_node = self._nodes.get(key)
            if tree_node:
                tree_node.add_assets({aid})
            else:
                tree_node = self.wrap_as_tree_node(node_key=key, tp=TreeNode.Type.OWNER, assets={aid})
                self.add_node(tree_node)
    
    def _compute_assets_amount(self):
        """
        生成数据结构:
        {
            "asset_id": set("node_key1", "node_key2" ...), # 资产所在的直接节点
        }
        迭代，对每个资产所在的节点的所有祖先节点取并集+去重, +1
        """
        aid_node_keys_mapper = defaultdict(set)
        for node in self._nodes.values():
            for aid in node.assets:
                aid_node_keys_mapper[aid].add(node.key)
        
        for aid, node_keys in aid_node_keys_mapper.items():
            ancestor_keys = set(self.get_ancestor_keys(node_keys)) # 必须去重
            for ancestor_key in ancestor_keys:
                tree_node = self._nodes.get(ancestor_key)
                if not tree_node:
                    continue
                tree_node.assets_amount_increment()
        
    def get_ancestor_keys(self, keys, with_self=True):
        ancestor_keys = set()
        for k in keys:
            _ancestor_keys = Node.get_node_ancestor_keys(k, with_self=with_self)
            ancestor_keys.update(_ancestor_keys)
        return ancestor_keys

    def _prune(self):
        self._prune_owner_nodes_branch()
    
    def _prune_owner_nodes_branch(self):
        # 修剪所有 owner nodes 节点的分枝（保留每条 owner 节点分枝的最上一层，删除其所有子孙节点）
        owner_node_keys = [n.key for n in self._owner_nodes]
        for node in list(self._nodes.values()):
            ancestor_keys = Node.get_node_ancestor_keys(node.key)
            if set(ancestor_keys) & set(owner_node_keys):
                self.remove_node(node)
    
    @property
    def _owner_nodes(self):
        return [node for node in self._nodes.values() if node.type == TreeNode.Type.OWNER]
    
    def add_node(self, node: TreeNode):
        _node = self._nodes.get(node.key)
        if _node is None:
            self._nodes[node.key] = node
            return
        if _node.can_be_overridden(node):
            self._nodes[node.key] = node
            return
    
    def remove_node(self, node_or_key: 'TreeNode | str'):
        if isinstance(node_or_key, TreeNode):
            key = node_or_key.key
        else:
            key = node_or_key
        self._nodes.pop(key, None)

    @classmethod
    def wrap_as_tree_node(cls, node_key, tp, assets=None):
        return TreeNode(key=node_key, tp=tp, assets=assets)
    
    @classmethod
    def wrap_as_tree_nodes(cls, node_keys, tp):
        return [cls.wrap_as_tree_node(nk, tp) for nk in node_keys]

    def print_nodes(self):
        print('--- Tree Nodes ---')
        for n in self._nodes.values():
            print(f'{n.key}({n.assets_amount}) - {n.type}')


class UserPermTreeEngine(object):
    """
        DA: Directly Permed Asset 
        DN: Directly Permed Node

        DA-Tree: 通过直接授权的资产生成的树
        DN-Tree: 通过直接授权的节点生成的树

        Perm-Tree: 最终的权限树，由 DA-Tree 和 DN-Tree 合并生成，bridge 和 da 节点全部保留，owner 节点只保留第一级

        Tree-Node-Type:
            bridge: 所有权桥梁节点，没有直接授权节点，也没有授权它下的资产
            owner: 所有权节点，直接授权的节点
            da: DA 节点，仅授权它下的资产
    """

    def __init__(self, user, org_id=None):
        self.user = user
        self._user_id = str(user.id)
        self._org_id = org_id or current_org.id

    def tree(self):
        da_tree = self._generate_da_tree()
        dn_tree = self._generate_dn_tree()
        tree = self._merge_trees(da_tree, dn_tree)
        return tree

    def _generate_da_tree(self):
        node_assets_mapper = self._get_da_node_key_asset_ids_mapper()
        tree_nodes = [
            TreeNode(key=key, tp=TreeNode.Type.DA, assets=asset_ids) 
            for key, asset_ids in node_assets_mapper.items()
        ]
        tree = Tree(nodes=tree_nodes)
        return tree

    def _get_da_node_key_asset_ids_mapper(self):
        direct_asset_ids = AssetPermission.assets.through.objects \
            .filter(assetpermission_id__in=self._perm_ids) \
            .annotate(char_id=F('asset_id')).values_list('char_id', flat=True)
        nid_aid_set = Asset.nodes.through.objects.filter(asset_id__in=direct_asset_ids) \
            .annotate(char_nid=F('node_id'), char_aid=F('asset_id')).values_list('char_nid', 'char_aid')
        nid_aid_mapper = dict(nid_aid_set)

        node_ids = list(nid_aid_mapper.keys())
        node_id_key_set = Node.objects.filter(id__in=node_ids).annotate(char_id=F('id')).values_list('id', 'key')
        node_id_key_mapper = dict(node_id_key_set)

        mapper = defaultdict(set)
        for nid, aid in nid_aid_set:
            key = node_id_key_mapper.get(nid)
            if key:
                mapper[key].add(aid)
        return mapper

    def _generate_dn_tree(self):
        node_keys = self._get_dn_node_keys()
        nodes = Tree.wrap_as_tree_nodes(node_keys, TreeNode.Type.OWNER)
        tree = Tree(nodes=nodes)
        return tree

    def _get_dn_node_keys(self):
        node_ids = AssetPermission.nodes.through.objects.filter(assetpermission_id__in=self._perm_ids) \
            .annotate(char_id=F('node_id')).values_list('char_id', flat=True)
        node_keys = Node.objects.filter(id__in=node_ids).values_list('key', flat=True)
        return list(set(node_keys))
    
    def _merge_trees(self, da_tree: Tree, dn_tree: Tree) -> Tree:
        tree = da_tree.merge(dn_tree)
        return tree

    @lazyproperty
    def _perm_ids(self):
        return self._get_permission_ids()

    def _get_permission_ids(self):
        user_perm_ids = AssetPermission.users.through.objects.filter(user_id=self._user_id).annotate(
            char_id=F('assetpermission_id')).values_list('char_id', flat=True)
        group_ids = User.groups.through.objects.filter(user_id=self._user_id).annotate(
            char_id=F('usergroup_id')).values_list('char_id', flat=True)
        group_perm_ids = AssetPermission.user_groups.through.objects.filter(usergroup_id__in=group_ids).annotate(
            char_id=F('assetpermission_id')).values_list('char_id', flat=True)
        perm_ids = set(user_perm_ids).union(set(group_perm_ids))
        return perm_ids
