import numpy as np
import torch
from functools import reduce
from collections import Counter
import scipy as sp
from sklearn.preprocessing import normalize

class Mesh:
    def __init__(self, path, build_mat=False):
        self.path = path
        self.vs, self.faces = self.fill_from_file(path)
        self.compute_face_normals()
        self.compute_face_center()
        self.device = 'cpu'
        self.build_gemm() #self.edges, self.ve
        self.compute_vert_normals()
        self.compute_fn_sphere()
        self.build_vf()
        if build_mat:
            self.build_uni_lap()
            self.build_mesh_lap()

    def fill_from_file(self, path):
        vs, faces = [], []
        f = open(path)
        for line in f:
            line = line.strip()
            splitted_line = line.split()
            if not splitted_line:
                continue
            elif splitted_line[0] == 'v':
                vs.append([float(v) for v in splitted_line[1:4]])
            elif splitted_line[0] == 'f':
                face_vertex_ids = [int(c.split('/')[0]) for c in splitted_line[1:]]
                assert len(face_vertex_ids) == 3
                face_vertex_ids = [(ind - 1) if (ind >= 0) else (len(vs) + ind) for ind in face_vertex_ids]
                faces.append(face_vertex_ids)
        f.close()
        vs = np.asarray(vs)
        faces = np.asarray(faces, dtype=int)

        assert np.logical_and(faces >= 0, faces < len(vs)).all()
        return vs, faces

    def build_gemm(self):
        self.ve = [[] for _ in self.vs]
        self.vei = [[] for _ in self.vs]
        edge_nb = []
        sides = []
        edge2key = dict()
        edges = []
        edges_count = 0
        nb_count = []
        for face_id, face in enumerate(self.faces):
            faces_edges = []
            for i in range(3):
                cur_edge = (face[i], face[(i + 1) % 3])
                faces_edges.append(cur_edge)
            for idx, edge in enumerate(faces_edges):
                edge = tuple(sorted(list(edge)))
                faces_edges[idx] = edge
                if edge not in edge2key:
                    edge2key[edge] = edges_count
                    edges.append(list(edge))
                    edge_nb.append([-1, -1, -1, -1])
                    sides.append([-1, -1, -1, -1])
                    self.ve[edge[0]].append(edges_count)
                    self.ve[edge[1]].append(edges_count)
                    self.vei[edge[0]].append(0)
                    self.vei[edge[1]].append(1)
                    nb_count.append(0)
                    edges_count += 1
            for idx, edge in enumerate(faces_edges):
                edge_key = edge2key[edge]
                edge_nb[edge_key][nb_count[edge_key]] = edge2key[faces_edges[(idx + 1) % 3]]
                edge_nb[edge_key][nb_count[edge_key] + 1] = edge2key[faces_edges[(idx + 2) % 3]]
                nb_count[edge_key] += 2
            for idx, edge in enumerate(faces_edges):
                edge_key = edge2key[edge]
                sides[edge_key][nb_count[edge_key] - 2] = nb_count[edge2key[faces_edges[(idx + 1) % 3]]] - 1
                sides[edge_key][nb_count[edge_key] - 1] = nb_count[edge2key[faces_edges[(idx + 2) % 3]]] - 2
        self.edges = np.array(edges, dtype=np.int32)
        self.gemm_edges = np.array(edge_nb, dtype=np.int64)
        self.sides = np.array(sides, dtype=np.int64)
        self.edges_count = edges_count
        # lots of DS for loss

        self.nvs, self.nvsi, self.nvsin, self.ve_in = [], [], [], []
        for i, e in enumerate(self.ve):
            self.nvs.append(len(e))
            self.nvsi += len(e) * [i]
            self.nvsin += list(range(len(e)))
            self.ve_in += e
        self.vei = reduce(lambda a, b: a + b, self.vei, [])
        self.vei = torch.from_numpy(np.array(self.vei).ravel()).to(self.device).long()
        self.nvsi = torch.from_numpy(np.array(self.nvsi).ravel()).to(self.device).long()
        self.nvsin = torch.from_numpy(np.array(self.nvsin).ravel()).to(self.device).long()
        self.ve_in = torch.from_numpy(np.array(self.ve_in).ravel()).to(self.device).long()

        self.max_nvs = max(self.nvs)
        self.nvs = torch.Tensor(self.nvs).to(self.device).float()
        self.edge2key = edge2key

    def compute_face_normals(self):
        face_normals = np.cross(self.vs[self.faces[:, 1]] - self.vs[self.faces[:, 0]], self.vs[self.faces[:, 2]] - self.vs[self.faces[:, 0]])
        norm = np.sqrt(np.sum(np.square(face_normals), 1))
        face_areas = 0.5 * np.sqrt((face_normals**2).sum(axis=1))
        face_normals /= np.tile(norm, (3, 1)).T
        self.fn, self.fa = face_normals, face_areas

    def compute_vert_normals(self):
        vert_normals = np.zeros((3, len(self.vs)))
        face_normals = self.fn
        faces = self.faces

        nv = len(self.vs)
        nf = len(faces)
        mat_rows = faces.reshape(-1)
        mat_cols = np.array([[i] * 3 for i in range(nf)]).reshape(-1)
        mat_vals = np.ones(len(mat_rows))
        f2v_mat = sp.sparse.csr_matrix((mat_vals, (mat_rows, mat_cols)), shape=(nv, nf))
        vert_normals = sp.sparse.csr_matrix.dot(f2v_mat, face_normals)
        vert_normals = normalize(vert_normals, norm='l2', axis=1)
        self.vn = vert_normals
    
    def compute_face_center(self):
        faces = self.faces
        vs = self.vs
        self.fc = np.sum(vs[faces], 1) / 3.0
    
    def compute_fn_sphere(self):
        fn = self.fn
        u = (np.arctan2(fn[:, 1], fn[:, 0]) + np.pi) / (2.0 * np.pi)
        v = np.arctan2(np.sqrt(fn[:, 0]**2 + fn[:, 1]**2), fn[:, 2]) / np.pi
        self.fn_uv = np.stack([u, v]).T
    
    def build_uni_lap(self):
        """compute uniform laplacian matrix"""
        vs = torch.tensor(self.vs.T, dtype=torch.float)
        edges = self.edges
        ve = self.ve

        sub_mesh_vv = [edges[v_e, :].reshape(-1) for v_e in ve]
        sub_mesh_vv = [set(vv.tolist()).difference(set([i])) for i, vv in enumerate(sub_mesh_vv)]

        num_verts = vs.size(1)
        mat_rows = [np.array([i] * len(vv), dtype=np.int64) for i, vv in enumerate(sub_mesh_vv)]
        mat_rows = np.concatenate(mat_rows)
        mat_cols = [np.array(list(vv), dtype=np.int64) for vv in sub_mesh_vv]
        mat_cols = np.concatenate(mat_cols)

        mat_rows = torch.from_numpy(mat_rows).long()
        mat_cols = torch.from_numpy(mat_cols).long()
        mat_vals = torch.ones_like(mat_rows).float() * -1.0
        neig_mat = torch.sparse.FloatTensor(torch.stack([mat_rows, mat_cols], dim=0),
                                            mat_vals,
                                            size=torch.Size([num_verts, num_verts]))
        vs = vs.T

        sum_count = torch.sparse.mm(neig_mat, torch.ones((num_verts, 1)).type_as(vs))
        mat_rows_ident = np.array([i for i in range(num_verts)])
        mat_cols_ident = np.array([i for i in range(num_verts)])
        mat_ident = np.array([-s for s in sum_count[:, 0]])
        mat_rows_ident = torch.from_numpy(mat_rows_ident).long()
        mat_cols_ident = torch.from_numpy(mat_cols_ident).long()
        mat_ident = torch.from_numpy(mat_ident).long()
        mat_rows = torch.cat([mat_rows, mat_rows_ident])
        mat_cols = torch.cat([mat_cols, mat_cols_ident])
        mat_vals = torch.cat([mat_vals, mat_ident])

        self.lapmat = torch.sparse.FloatTensor(torch.stack([mat_rows, mat_cols], dim=0),
                                            mat_vals,
                                            size=torch.Size([num_verts, num_verts]))
    
    def build_vf(self):
        vf = [set() for _ in range(len(self.vs))]
        for i, f in enumerate(self.faces):
            vf[f[0]].add(i)
            vf[f[1]].add(i)
            vf[f[2]].add(i)
        self.vf = vf
        
        f2f = [[] for _ in range(len(self.faces))]
        f_edges = np.array([[i] * 3 for i in range(len(self.faces))])
        #f_edges_ext = [[] for _ in range(2)]
        for i, f in enumerate(self.faces):
            all_neig = list(vf[f[0]]) + list(vf[f[1]]) + list(vf[f[2]])
            neig_f, _ = zip(*Counter(all_neig).most_common(4)[1:])
            #neig_f_ext, _ = zip(*Counter(all_neig).most_common()[1:])
            f2f[i] = list(neig_f)
            #f_edges_ext[0] = f_edges_ext[0] + list(neig_f_ext)
            #f_edges_ext[1] = f_edges_ext[1] + [i] * len(neig_f_ext)

        self.f2f = np.array(f2f)
        #f_edges_ext = np.array(f_edges_ext)
        
        self.f_edges = np.concatenate((self.f2f.reshape(1, -1), f_edges.reshape(1, -1)), 0)
        mat_inds = torch.from_numpy(self.f_edges).long()
        #mat_vals = torch.ones(mat_inds.shape[1]).float()
        mat_vals = torch.from_numpy(self.fa[self.f_edges[0]]).float()
        #mat_inds_ident = torch.arange(len(self.faces)).repeat(2).reshape(2, -1)
        #mat_vals_ident = torch.ones(len(self.faces)).float() * 3.0
        #mat_inds = torch.cat([mat_inds, mat_inds_ident], dim=1)
        #mat_vals = torch.cat([mat_vals, mat_vals_ident], dim=0)
        self.f2f_mat = torch.sparse.FloatTensor(mat_inds, mat_vals, size=torch.Size([len(self.faces), len(self.faces)]))
        
        #mat_inds_ext = torch.from_numpy(f_edges_ext).long()
        #mat_vals_ext = torch.from_numpy(self.fa[f_edges_ext[0]]).float()
        #self.f2f_mat_ext = torch.sparse.FloatTensor(mat_inds_ext, mat_vals_ext, size=torch.Size([len(self.faces), len(self.faces)]))
        
    def build_mesh_lap(self):
        """compute mesh laplacian matrix"""
        vs = self.vs
        vf = self.vf
        fa = self.fa
        edges = self.edges
        faces = self.faces
        e_dict = {}
        
        for e in edges:
            e0 = min(e)
            e1 = max(e)
            e_dict[(e0, e1)] = []
        """
        for v in range(len(vs)):
            n_f = vf[v]
            for f in n_f:
                n_v = faces[f]
                if n_v[1] == v:
                    n_v = n_v[[1,2,0]]
                elif n_v[2] == v:
                    n_v = n_v[[2,1,0]]
                s = vs[n_v[1]] - vs[n_v[0]]
                t = vs[n_v[2]] - vs[n_v[1]]
                u = vs[n_v[0]] - vs[n_v[2]]
                i1 = np.inner(-s, t)
                i2 = np.inner(-t, u)
                n1 = np.linalg.norm(s) * np.linalg.norm(t)
                n2 = np.linalg.norm(t) * np.linalg.norm(u)
                c1 = np.clip(i1 / n1, -1.0, 1.0)
                c2 = np.clip(i2 / n2, -1.0, 1.0)
                cot1 = c1 / np.sqrt(1 - c1 ** 2)
                cot2 = c2 / np.sqrt(1 - c2 ** 2)
                keys1 = (min(n_v[0], n_v[1]), max(n_v[0], n_v[1]))
                keys2 = (min(n_v[0], n_v[2]), max(n_v[0], n_v[2]))
                e_dict[keys1].append(cot2)
                e_dict[keys2].append(cot1)

        for e in e_dict:
            e_dict[e] = -0.25 * (e_dict[e][0] + e_dict[e][1] + e_dict[e][2] + e_dict[e][3])
        """
        for f in faces:
            s = vs[f[1]] - vs[f[0]]
            t = vs[f[2]] - vs[f[1]]
            u = vs[f[0]] - vs[f[2]]
            cos_0 = np.inner(s, -u) / (np.linalg.norm(s) * np.linalg.norm(u))
            cos_1 = np.inner(t, -s) / (np.linalg.norm(t) * np.linalg.norm(s)) 
            cos_2 = np.inner(u, -t) / (np.linalg.norm(u) * np.linalg.norm(t))
            cot_0 = cos_0 / (np.sqrt(1 - cos_0 ** 2) + 1e-12)
            cot_1 = cos_1 / (np.sqrt(1 - cos_1 ** 2) + 1e-12)
            cot_2 = cos_2 / (np.sqrt(1 - cos_2 ** 2) + 1e-12)
            key_0 = (min(f[1], f[2]), max(f[1], f[2]))
            key_1 = (min(f[2], f[0]), max(f[2], f[0]))
            key_2 = (min(f[0], f[1]), max(f[0], f[1]))
            e_dict[key_0].append(cot_0)
            e_dict[key_1].append(cot_1)
            e_dict[key_2].append(cot_2)
        
        for e in e_dict:
            e_dict[e] = -0.5 * (e_dict[e][0] + e_dict[e][1])

        C_ind = [[], []]
        C_val = []
        ident = [0] * len(vs)
        for e in e_dict:
            C_ind[0].append(e[0])
            C_ind[1].append(e[1])
            C_ind[0].append(e[1])
            C_ind[1].append(e[0])
            C_val.append(e_dict[e])
            C_val.append(e_dict[e])
            ident[e[0]] += -1.0 * e_dict[e]
            ident[e[1]] += -1.0 * e_dict[e]
        for i in range(len(vs)):
            C_ind[0].append(i)
            C_ind[1].append(i)
        C_val = C_val + ident
        C_ind = torch.LongTensor(C_ind)
        C_val = torch.FloatTensor(C_val)
        # cotangent matrix
        C = torch.sparse.FloatTensor(C_ind, C_val, torch.Size([len(vs), len(vs)]))
        self.cot_mat = C

        M_ind = torch.stack([torch.arange(len(vs)), torch.arange(len(vs))], dim=0).long()
        M_val = []
        for v in range(len(vs)):
            faces = list(vf[v])
            va = 3.0 / (sum(fa[faces]) + 1e-12)
            M_val.append(va)
        M_val = torch.FloatTensor(M_val)
        # diagonal mass inverse matrix
        Minv = torch.sparse.FloatTensor(M_ind, M_val, torch.Size([len(vs), len(vs)]))
        C = torch.sparse.mm(Minv, C.to_dense()).to_sparse()
        self.mesh_lap = C
    
    def save(self, filename):
        assert len(self.vs) > 0
        vertices = np.array(self.vs, dtype=np.float32).flatten()
        indices = np.array(self.faces, dtype=np.uint32).flatten()

        with open(filename, 'w') as fp:
            # Write positions
            for i in range(0, vertices.size, 3):
                x = vertices[i + 0]
                y = vertices[i + 1]
                z = vertices[i + 2]
                fp.write('v {0:.8f} {1:.8f} {2:.8f}\n'.format(x, y, z))

            # Write indices
            for i in range(0, len(indices), 3):
                i0 = indices[i + 0] + 1
                i1 = indices[i + 1] + 1
                i2 = indices[i + 2] + 1
                fp.write('f {0} {1} {2}\n'.format(i0, i1, i2))