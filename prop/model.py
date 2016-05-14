import tensorflow as tf
# from tensorflow.python.ops.rnn import dynamic_rnn
from tensorflow.python.ops.rnn_cell import MultiRNNCell

from directed.base_model import BaseTower, BaseRunner
from my.tensorflow import flatten, exp_mask, translate
from my.tensorflow.nn import linear, relu1, dists
from my.tensorflow.rnn import dynamic_rnn, dynamic_bidirectional_rnn
import numpy as np

from my.tensorflow.rnn_cell import RSMCell, GRUCell, TempCell, BiDropoutWrapper, DropoutWrapper, PassingCell


class Embedder(object):
    def __call__(self, content):
        raise Exception()


class VariableEmbedder(Embedder):
    def __init__(self, params, wd=0.0, initializer=None, name="variable_embedder"):
        V, d = params.vocab_size, params.hidden_size
        with tf.variable_scope(name):
            self.emb_mat = tf.get_variable("emb_mat", dtype='float', shape=[V, d], initializer=initializer)
            # TODO : not sure wd is appropriate for embedding matrix
            if wd:
                weight_decay = tf.mul(tf.nn.l2_loss(self.emb_mat), wd, name='weight_loss')
                tf.add_to_collection('losses', weight_decay)

    def __call__(self, word, name="embedded_content"):
        out = tf.nn.embedding_lookup(self.emb_mat, word, name=name)
        return out


class PositionEncoder(object):
    def __init__(self, max_sent_size, hidden_size):
        self.max_sent_size, self.hidden_size = max_sent_size, hidden_size
        J, d = max_sent_size, hidden_size
        with tf.name_scope("pe_constants"):
            b = [1 - k/d for k in range(1, d+1)]
            w = [[j*(2*k/d - 1) for k in range(1, d+1)] for j in range(1, J+1)]
            self.b = tf.constant(b, shape=[d])
            self.w = tf.constant(w, shape=[J, d])

    def __call__(self, Ax, mask, scope=None):
        with tf.name_scope(scope or "position_encoder"):
            shape = Ax.get_shape().as_list()
            length_dim_index = len(shape) - 2
            length = tf.reduce_sum(tf.cast(mask, 'float'), length_dim_index)
            length = tf.maximum(length, 1.0)  # masked sentences will have length 0
            length_aug = tf.expand_dims(tf.expand_dims(length, -1), -1)
            # l = self.b + self.w/length_aug
            l = self.b + self.w/self.max_sent_size
            mask_aug = tf.expand_dims(mask, -1)
            f = tf.reduce_sum(Ax * l * tf.cast(mask_aug, 'float'), length_dim_index, name='f')  # [N, S, d]

            return f


class Tower(BaseTower):
    def initialize(self):
        params = self.params
        placeholders = self.placeholders
        tensors = self.tensors
        variables_dict = self.variables_dict
        N, J, V, Q, M = params.batch_size, params.max_sent_size, params.vocab_size, params.max_ques_size, params.mem_size
        d = params.hidden_size
        L = params.mem_num_layers
        forget_bias = params.forget_bias
        wd = params.wd
        with tf.name_scope("placeholders"):
            x = tf.placeholder('int32', shape=[N, M, J], name='x')
            x_mask = tf.placeholder('bool', shape=[N, M, J], name='x_mask')
            q = tf.placeholder('int32', shape=[N, J], name='q')
            q_mask = tf.placeholder('bool', shape=[N, J], name='q_mask')
            y = tf.placeholder('int32', shape=[N], name='y')
            is_train = tf.placeholder('bool', shape=[], name='is_train')
            placeholders['x'] = x
            placeholders['x_mask'] = x_mask
            placeholders['q'] = q
            placeholders['q_mask'] = q_mask
            placeholders['y'] = y
            placeholders['is_train'] = is_train

        with tf.variable_scope("embedding"):
            A = VariableEmbedder(params, wd=wd, name='A')
            Aq = A(q, name='Aq')  # [N, S, J, d]
            Ax = A(x, name='Ax')  # [N, S, J, d]

        with tf.name_scope("encoding"):
            encoder = PositionEncoder(J, d)
            u = encoder(Aq, q_mask)  # [N, d]
            m = encoder(Ax, x_mask)  # [N, M, d]

        with tf.variable_scope("networks"):
            m_mask = tf.reduce_max(tf.cast(x_mask, 'int64'), 2, name='m_mask')  # [N, M]
            m_length = tf.reduce_sum(m_mask, 1, name='m_length')  # [N]
            initializer = tf.random_uniform_initializer(-np.sqrt(3), np.sqrt(3))
            cell = RSMCell(d, forget_bias=forget_bias, wd=wd, initializer=initializer)
            us = tf.tile(tf.expand_dims(u, 1, name='u_prev_aug'), [1, M, 1])  # [N, d] -> [N, M, d]
            in_ = tf.concat(2, [tf.ones([N, M, 1]), m, us, tf.zeros([N, M, 2*d])], name='x_h_in')  # [N, M, 4*d + 1]
            out_, fw_state, bw_state, bi_tensors = dynamic_bidirectional_rnn(cell, in_,
                sequence_length=m_length, dtype='float', num_layers=L)
            a = tf.slice(out_, [0, 0, 0], [-1, -1, 1])  # [N, M, 1]
            # FIXME : g is not propagated! use c for passing cell!
            _, _, v, g = tf.split(2, 4, tf.slice(out_, [0, 0, 1], [-1, -1, -1]))
            fw_c, fw_h = tf.split(1, 2, tf.slice(fw_state, [0, 1], [-1, -1]))

            tensors['a'] = tf.squeeze(tf.slice(bi_tensors['in'], [0, 0, 0, 0], [-1, -1, -1, 1]), [3])
            tensors['of'] = tf.squeeze(tf.slice(bi_tensors['fw_out'], [0, 0, 0, 0], [-1, -1, -1, 1]), [3])
            tensors['ob'] = tf.squeeze(tf.slice(bi_tensors['bw_out'], [0, 0, 0, 0], [-1, -1, -1, 1]), [3])

        with tf.variable_scope("selection"):
            passing_cell = PassingCell(d)
            mid_in = tf.concat(2, [a, g])
            bw_out_rev, _ = dynamic_rnn(passing_cell, tf.reverse_sequence(mid_in, m_length, 1),
                                        sequence_length=m_length, dtype='float')
            bw_out = tf.reverse_sequence(bw_out_rev, m_length, 1)
            g_next = translate(bw_out, [0, -1, 0])

            s_raw = linear([g_next * us], 1, True, initializer=self.initializer, scope='s_raw')
            s = tf.nn.sigmoid(s_raw - 1.0) * a
            final_in = tf.concat(2, [s, g])
            final_out, final_state = dynamic_rnn(passing_cell, final_in, sequence_length=m_length, dtype='float')
            tensors['s'] = tf.squeeze(s, [2])
            w = tf.tanh(linear([final_state], d, True, wd=wd, scope='w_raw'))
            """
            temp_cell = TempCell(d, wd=wd)
            temp_in = tf.concat(2, [a, g, us])  # [N, M, 2*d + 1]
            temp_out, temp_state = dynamic_rnn(temp_cell, temp_in, sequence_length=m_length, dtype='float')
            tensors['s'] = tf.squeeze(temp_out, [2])
            c, h = tf.split(1, 2, temp_state)
            w = tf.tanh(linear([h], d, True, wd=wd))
            """

        with tf.variable_scope("class"):
            W = tf.transpose(A.emb_mat, name='W')
            logits = tf.matmul(w, W, name='logits')
            yp = tf.cast(tf.argmax(logits, 1), 'int32')
            correct = tf.equal(yp, y)
            tensors['yp'] = yp
            tensors['correct'] = correct

        with tf.name_scope("loss"):
            with tf.name_scope("ans_loss"):
                ce = tf.nn.sparse_softmax_cross_entropy_with_logits(logits, y, name='ce')
                avg_ce = tf.reduce_mean(ce, name='avg_ce')
                tf.add_to_collection('losses', avg_ce)

            losses = tf.get_collection('losses')
            loss = tf.add_n(losses, name='loss')
            tensors['loss'] = loss

        variables_dict['all'] = tf.trainable_variables()

    def get_feed_dict(self, batch, mode, **kwargs):
        params = self.params
        N, J, V, M = params.batch_size, params.max_sent_size, params.vocab_size, params.mem_size
        x = np.zeros([N, M, J], dtype='int32')
        x_mask = np.zeros([N, M, J], dtype='bool')
        q = np.zeros([N, J], dtype='int32')
        q_mask = np.zeros([N, J], dtype='bool')
        y = np.zeros([N], dtype='int32')

        ph = self.placeholders
        feed_dict = {ph['x']: x, ph['x_mask']: x_mask,
                     ph['q']: q, ph['q_mask']: q_mask,
                     ph['y']: y,
                     ph['is_train']: mode == 'train'
                     }
        if batch is None:
            return feed_dict

        X, Q, S, Y, H, T = batch
        for i, para in enumerate(X):
            if len(para) > M:
                para = para[-M:]
            for jj, sent in enumerate(para):
                # j = len(para) - jj - 1  # reverting story sequence, last to first
                j = jj
                for k, word in enumerate(sent):
                    x[i, j, k] = word
                    x_mask[i, j, k] = True

        for i, ques in enumerate(Q):
            for j, word in enumerate(ques):
                q[i, j] = word
                q_mask[i, j] = True

        for i, ans in enumerate(Y):
            y[i] = ans

        return feed_dict


class Runner(BaseRunner):
    def _get_train_op(self, **kwargs):
        return self.train_ops['all']