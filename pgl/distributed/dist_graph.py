# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
    This package implement the Distributed CPU Graph for 
    handling large scale graph data.
"""

import os
import sys
import time
import argparse
import numpy as np

from paddle.fluid.core import GraphPyService, GraphPyServer, GraphPyClient
from pgl.utils.logger import log

from pgl.distributed import helper

__all__ = ['DistGraphServer', 'DistGraphClient']


def stream_shuffle_generator(dataloader,
                             server_idx,
                             batch_size,
                             shuffle_size=20000):
    """
    Args:
        dataloader: iterable dataset

        server_idx: int

        batch_size: int

        shuffle_size: int

    """
    buffer_list = []
    for nodes in dataloader(server_idx):
        if len(buffer_list) < shuffle_size:
            buffer_list.extend(nodes)
        else:
            random_ids = np.random.choice(
                len(buffer_list), size=len(nodes), replace=False)
            batch_nodes = [buffer_list[i] for i in random_ids]
            for ii, nid in enumerate(nodes):
                buffer_list[random_ids[ii]] = nid

            yield batch_nodes

    if len(buffer_list) > 0:
        np.random.shuffle(buffer_list)
        start = 0
        while True:
            batch_nodes = buffer_list[start:(start + batch_size)]
            start += batch_size
            if len(batch_nodes) > 0:
                yield batch_nodes
            else:
                break


class DistGraphServer(object):
    def __init__(self, config, shard_num, ip_config, server_id):
        """
        Args:
            config: yaml configure file

            shard_num: int, the sharding number of graph data

            ip_config: list of IP address or a path of IP configuration file
            
            For example, the following TXT shows a 4-machine configuration:

                172.31.50.123:8245
                172.31.50.124:8245
                172.31.50.125:8245
                172.31.50.126:8245

            server_id: int 

        """
        self.config = helper.load_config(config)
        self.shard_num = shard_num
        self.server_id = server_id

        if self.config.symmetry:
            self.symmetry = self.config.symmetry
        else:
            self.symmetry = False

        if isinstance(ip_config, str):
            self.ip_addr = helper.load_ip_addr(ip_config)
        elif isinstance(ip_config, list):
            self.ip_addr = ";".join(ip_config)
        else:
            raise TypeError("ip_config should be list of IP address or "
                            "a path of IP configuration file. "
                            "But got %s" % (type(ip_config)))

        self.ntype2files = helper.parse_files(self.config.ntype2files)
        self.node_type_list = list(self.ntype2files.keys())

        self.etype2files = helper.parse_files(self.config.etype2files)
        self.edge_type_list = helper.get_all_edge_type(self.etype2files,
                                                       self.symmetry)

        self._server = GraphPyServer()
        self._server.set_up(self.ip_addr, self.shard_num, self.node_type_list,
                            self.edge_type_list, self.server_id)

        if self.config.nfeat_info:
            for item in self.config.nfeat_info:
                self._server.add_table_feat_conf(*item)
        self._server.start_server()


class DistGraphClient(object):
    def __init__(self, config, shard_num, server_num, ip_config, client_id):
        """
        Args:
            config: yaml configure file

            shard_num: int, the sharding number of graph data

            server_num: int, total number of server

            ip_config: list of IP address or a path of IP configuration file
            
            For example, the following TXT shows a 4-machine configuration:

                172.31.50.123:8245
                172.31.50.124:8245
                172.31.50.125:8245
                172.31.50.126:8245

            client_id: int 

        """
        self.config = helper.load_config(config)
        self.shard_num = shard_num
        self.server_num = server_num
        self.client_id = client_id

        if self.config.symmetry:
            self.symmetry = self.config.symmetry
        else:
            self.symmetry = False

        if self.config.node_batch_stream_shuffle_size:
            self.stream_shuffle_size = self.config.node_batch_stream_shuffle_size
        else:
            warnings.warn("node_batch_stream_shuffle_size is not specified, "
                          "default value is 20000")
            self.stream_shuffle_size = 20000

        if isinstance(ip_config, str):
            self.ip_addr = helper.load_ip_addr(ip_config)
        elif isinstance(ip_config, list):
            self.ip_addr = ";".join(ip_config)
        else:
            raise TypeError("ip_config should be list of IP address or "
                            "a path of IP configuration file. "
                            "But got %s" % (type(ip_config)))

        if self.config.nfeat_info is not None:
            self.nfeat_info = helper.convert_nfeat_info(self.config.nfeat_info)
        else:
            self.nfeat_info = None

        self.ntype2files = helper.parse_files(self.config.ntype2files)
        self.node_type_list = list(self.ntype2files.keys())

        self.etype2files = helper.parse_files(self.config.etype2files)
        self.edge_type_list = helper.get_all_edge_type(self.etype2files,
                                                       self.symmetry)

        self._client = GraphPyClient()
        self._client.set_up(self.ip_addr, self.shard_num, self.node_type_list,
                            self.edge_type_list, self.client_id)
        self._client.start_client()

    def load_edges(self):
        for etype, file_or_dir in self.etype2files.items():
            file_list = [f for f in helper.get_files(file_or_dir)]
            filepath = ";".join(file_list)
            log.info("load edges of type %s from %s" % (etype, filepath))
            self._client.load_edge_file(etype, filepath, False)
            if self.symmetry:
                r_etype = helper.get_inverse_etype(etype)
                self._client.load_edge_file(r_etype, filepath, True)

    def load_node_types(self):
        for ntype, file_or_dir in self.ntype2files.items():
            file_list = [f for f in helper.get_files(file_or_dir)]
            filepath = ";".join(file_list)
            log.info("load nodes of type %s from %s" % (ntype, filepath))
            self._client.load_node_file(ntype, filepath)

    def sample_predecessor(self, nodes, max_degree, edge_type):
        out = self.sample_successor(nodes, max_degree, edge_type)
        return out

    def sample_successor(self, nodes, max_degree, edge_type):
        """
        Args:
            nodes: list of node ID

            max_degree: int, sample number of each node

            edge_type: str, edge type
        """
        res = self._client.batch_sample_neighboors(edge_type, nodes,
                                                   max_degree)
        neighs = [[] for _ in range(len(res))]
        for idx, n_neighs in enumerate(res):
            for pair in n_neighs:
                neighs[idx].append(pair[0])
        return neighs

    def random_sample_nodes(self, node_type, size):
        """

        Args:
            node_type: str,

            size: int
        """
        server_idx = np.random.choice(self.server_num)
        nodes = self._client.random_sample_nodes(node_type, server_idx, size)

        return nodes

    def node_batch_iter(self,
                        batch_size,
                        node_type,
                        shuffle=True,
                        rank=0,
                        nrank=1):
        def _batch_data_generator(server_idx):
            start = rank * batch_size
            while True:
                res = self._client.pull_graph_list(node_type, server_idx,
                                                   start, batch_size, 1)
                start += (nrank * batch_size)
                nodes = [x.get_id() for x in res]

                if len(nodes) > 0:
                    yield nodes
                if len(nodes) != batch_size:
                    break

        server_idx_list = list(range(self.server_num))
        np.random.shuffle(server_idx_list)
        for server_idx in server_idx_list:
            if shuffle:
                for nodes in stream_shuffle_generator(
                        _batch_data_generator, server_idx, batch_size,
                        self.stream_shuffle_size):
                    yield nodes
            else:
                for nodes in _batch_data_generator(server_idx):
                    yield nodes

    def get_node_feat(self, nodes, node_type, feat_names):
        """
        Args:
            nodes: list of node ID

            node_type: str, node type

            feat_names: the node feature name or a list of node feature name
        """

        flag = False
        if isinstance(feat_names, str):
            feat_names = [feat_names]
            flag = True
        elif isinstance(feat_names, list):
            pass
        else:
            raise TypeError(
                "The argument of feat_names should a node feature name "
                "or a list of node feature name. "
                "But got %s" % (type(feat_names)))

        byte_nfeat_list = self._client.get_node_feat(node_type, nodes,
                                                     feat_names)

        # convert bytes to dtype
        nfeat_list = []
        for fn_idx, fn in enumerate(feat_names):
            dtype, _ = self.nfeat_info[node_type][fn]
            if dtype == "string":
                f_list = [
                    item.decode("utf-8") for item in byte_nfeat_list[fn_idx]
                ]
            else:
                f_list = [
                    np.frombuffer(item, dtype)
                    for item in byte_nfeat_list[fn_idx]
                ]
            nfeat_list.append(f_list)

        if flag:
            return nfeat_list[0]
        else:
            return nfeat_list

    def stop_server(self):
        self._client.stop_server()
