from django.db.models import F
from typing import Optional
from collections import defaultdict

from users.models import UserGroup, User
from assets.models import Asset, Node
from perms.models import AssetPermission

from common.utils import lazyproperty


class TreeNode:

    class Type:
        BRIDGE = 'bridge'
        OWNER = 'owner'
        DA = 'da'

    def __init__(self, key, tp):
        self.key = key
        self.type = tp

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

    def __init__(self, nodes: Optional[list[TreeNode]] = None):
        self._nodes = defaultdict(TreeNode)
        self.init(nodes)
    
    def init(self, nodes: Optional[list[TreeNode]]):
        if nodes is None:
            return
        for node in nodes:
            self.add_node(node)
        self._reverse_generated()
        self._sorted()
    
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
        merged_tree._sorted()
        return merged_tree
    
    def _sorted(self):
        self._nodes = defaultdict(
            TreeNode, 
            sorted(self._nodes.items(), key=lambda item: [int(i) for i in item[0].split(':')])
        )
    
    def _prune(self):
        self._prune_owner_nodes_branch()
    
    def _prune_owner_nodes_branch(self):
        # 修剪所有 owner nodes 节点的分枝（保留每条 owner 节点分枝的最上一层，删除其所有子孙节点）
        owner_node_keys = set([
            node.key for node in self._nodes.values() if node.type == TreeNode.Type.OWNER
        ])
        for node in list(self._nodes.values()):
            ancestor_keys = Node.get_node_ancestor_keys(node.key)
            if set(ancestor_keys) & owner_node_keys:
                self.remove_node(node)
    
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
    def wrap_as_tree_nodes(cls, node_keys, tp):
        return [TreeNode(key=nk, tp=tp) for nk in node_keys]


    def print_nodes(self):
        for n in self._nodes.values():
            print(n.key, n.type)


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

    def __init__(self, user):
        self.user = user
        self._user_id = str(user.id)

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
        node_keys = self._get_da_node_keys()
        nodes = Tree.wrap_as_tree_nodes(node_keys, TreeNode.Type.DA)
        tree = Tree(nodes=nodes)
        return tree

    def _get_da_node_keys(self):
        direct_asset_ids = self._get_directly_asset_ids()
        node_ids = Asset.nodes.through.objects.filter(asset_id__in=direct_asset_ids).annotate(
            char_id=F('node_id')).values_list('char_id', flat=True)
        node_keys = Node.objects.filter(id__in=node_ids).values_list('key', flat=True)
        return list(set(node_keys))

    def _get_directly_asset_ids(self):
        asset_ids = AssetPermission.assets.through.objects.filter(assetpermission_id__in=self._perm_ids)\
            .annotate(char_id=F('asset_id')).values_list('char_id', flat=True)
        return asset_ids

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
