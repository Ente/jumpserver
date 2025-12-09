from django.db.models import F
from typing import Optional
from collections import defaultdict

from users.models import UserGroup, User
from assets.models import Asset, Node
from assets.utils.node import NodeAssetsUtil
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
        self._assets = set() if assets is None else set(assets)
        self._assets_amount = 0
    
    
    def add_assets(self, asset_ids):
        self._assets.update(asset_ids)
    
    @property
    def assets_amount(self):
        return self._assets_amount

    @assets_amount.setter
    def assets_amount(self, amount):
        self._assets_amount = amount

    @property
    def assets(self):
        return self._assets
    
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
        self._nodes = defaultdict(TreeNode)
        self._org_id = org_id
        self.init(nodes)
    
    def init(self, nodes: Optional[list[TreeNode]]):
        if nodes is None:
            return
        for node in nodes:
            self.add_node(node)
        self._reverse_generated()
        self._finalize()
    
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
        merged_tree._prune()
        merged_tree._finalize()
        return merged_tree
    
    def _finalize(self):
        self._sorted()
        self._init_owner_nodes_assets()
        self._compute_assets_amount()
    
    def _sorted(self):
        self._nodes = defaultdict(
            TreeNode, 
            sorted(self._nodes.items(), key=lambda item: [int(i) for i in item[0].split(':')])
        )
    
    def _init_owner_nodes_assets(self):
        mapper = Node.get_node_all_asset_ids_mapping(org_id=self._org_id)
        for node in self._owner_nodes.values():
            asset_ids = mapper.get(node.key, set())
            node.add_assets(asset_ids)
    
    def _compute_assets_amount(self):
        mapper = {node.key: node.assets for node in self._nodes.values()}

        util = NodeAssetsUtil(nodes=self._nodes.values(), nodekey_assetsid_mapper=mapper)
        util.generate()
        for node in self._nodes.values():
            node.assets_amount = util.get_assets_amount(node.key)
        
    def _prune(self):
        self._prune_owner_nodes_branch()
    
    def _prune_owner_nodes_branch(self):
        # 修剪所有 owner nodes 节点的分枝（保留每条 owner 节点分枝的最上一层，删除其所有子孙节点）
        owner_node_keys = list(self._owner_nodes.keys())
        for node in list(self._nodes.values()):
            ancestor_keys = Node.get_node_ancestor_keys(node.key)
            if set(ancestor_keys) & set(owner_node_keys):
                self.remove_node(node)
    
    @property
    def _owner_nodes(self):
        return {
            key: node for key, node in self._nodes.items() if node.type == TreeNode.Type.OWNER
        }
    
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
        print("DA Tree Nodes:")
        da_tree.print_nodes()
        dn_tree = self._generate_dn_tree()
        print("DN Tree Nodes:")
        dn_tree.print_nodes()
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
        node_asset_ids = Asset.nodes.through.objects.filter(asset_id__in=direct_asset_ids).annotate(
            char_nid=F('node_id'), char_aid=F('asset_id')).values_list('char_nid', 'char_aid')
        
        node_ids = dict(node_asset_ids).keys()
        id_key_mapper= dict(Node.objects.filter(id__in=node_ids).annotate(char_id=F('id')).values_list('id', 'key'))

        mapper = defaultdict(set)
        for nid, aid in node_asset_ids:
            key = id_key_mapper.get(nid)
            if not key:
                continue
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
