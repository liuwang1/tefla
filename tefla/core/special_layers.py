import tensorflow as tf
from past.builtins import xrange
from tefla.core.layers import conv2d, batch_norm_tf as batch_norm
from tefla.utils import util
from tefla.core import initializers as initz


def spatialtransformer(U, theta, batch_size=64, downsample_factor=1.0, num_transform=1, name='SpatialTransformer', **kwargs):
    """Spatial Transformer Layer

    Implements a spatial transformer layer as described in [1]_.
    It's based on lasagne implementation in [2]_, modified by Mrinal Haloi

    Args:
        U: float
            The output of a convolutional net should have the
            shape [batch_size, height, width, num_channels].
        theta: float
            The output of the localisation network should be [batch_size, num_transform, 6] or [batch_size, 6] if num_transform=1
            ```python
                `theta`` to :
                    identity = np.array([[1., 0., 0.],
                                     [0., 1., 0.]])
                    identity = identity.flatten()
                    theta = tf.Variable(initial_value=identity)
            ```
        downsample_factor: a float, determines output shape, downsample input shape by downsample_factor

    Returns:
        spatial transformed output of the network

    References
    .. [1]  "Spatial Transformer Networks", Max Jaderberg, Karen Simonyan, Andrew Zisserman, Koray Kavukcuoglu
    .. [2]  https://github.com/skaae/transformer_network/blob/master/transformerlayer.py


    """
    with tf.variable_scope(name):
        if num_transform > 1 and len(theta.get_shape().as_list()) == 3:
            _, num_transforms = map(int, theta.get_shape().as_list()[:2])
            indices = [[i] * num_transforms for i in range(batch_size)]
            U = tf.gather(U, tf.reshape(indices, [-1]))

        input_shape = U.get_shape().as_list()
        num_channels = input_shape[3]
        theta = tf.reshape(theta, (-1, 2, 3))
        theta = tf.cast(theta, tf.float32)
        if not isinstance(downsample_factor, float):
            downsample_factor = tf.cast(downsample_factor, tf.float32)

        # grid of (x_t, y_t, 1), eq (1) in ref [1]
        out_height = tf.cast(input_shape[1] / downsample_factor, tf.int32)
        out_width = tf.cast(input_shape[2] / downsample_factor, tf.int32)
        grid = _meshgrid(out_height, out_width)
        grid = tf.expand_dims(grid, 0)
        grid = tf.reshape(grid, [-1])
        grid = tf.tile(grid, tf.pack([batch_size]))
        grid = tf.reshape(grid, tf.pack([batch_size, 3, -1]))

        # Transform A x (x_t, y_t, 1)^T -> (x_s, y_s)
        T_g = tf.batch_matmul(theta, grid)
        x_s = tf.slice(T_g, [0, 0, 0], [-1, 1, -1])
        y_s = tf.slice(T_g, [0, 1, 0], [-1, 1, -1])
        x_s_flat = tf.reshape(x_s, [-1])
        y_s_flat = tf.reshape(y_s, [-1])

        input_transformed = _interpolate(
            U, x_s_flat, y_s_flat, batch_size, downsample_factor)

        output = tf.reshape(input_transformed, tf.pack(
            [batch_size, out_height, out_width, num_channels]))
        return output


def _repeat(x, n_repeats):
    with tf.variable_scope('_repeat'):
        rep = tf.transpose(tf.expand_dims(
            tf.ones(shape=tf.pack([n_repeats, ])), 1), [1, 0])
        rep = tf.cast(rep, tf.int32)
        x = tf.matmul(tf.reshape(x, (-1, 1)), rep)
        return tf.reshape(x, [-1])


def _interpolate(im, x, y, batch_size, downsample_factor):
    with tf.variable_scope('_interpolate'):
        input_shape = im.get_shape().as_list()
        height = input_shape[1]
        width = input_shape[2]
        channels = input_shape[3]

        x = tf.cast(x, tf.float32)
        y = tf.cast(y, tf.float32)
        height_f = tf.cast(height, tf.float32)
        width_f = tf.cast(width, tf.float32)
        out_height = tf.cast(height / downsample_factor, tf.int32)
        out_width = tf.cast(width / downsample_factor, tf.int32)
        zero = tf.zeros([], dtype=tf.int32)
        max_y = tf.cast(height - 1, tf.int32)
        max_x = tf.cast(width - 1, tf.int32)

        # scale indices from [-1, 1] to [0, width/height]
        x = (x + 1.0) * (width_f) / 2.0
        y = (y + 1.0) * (height_f) / 2.0

        # do sampling
        x0 = tf.cast(tf.floor(x), tf.int32)
        x1 = x0 + 1
        y0 = tf.cast(tf.floor(y), tf.int32)
        y1 = y0 + 1

        x0 = tf.clip_by_value(x0, zero, max_x)
        x1 = tf.clip_by_value(x1, zero, max_x)
        y0 = tf.clip_by_value(y0, zero, max_y)
        y1 = tf.clip_by_value(y1, zero, max_y)
        dim2 = width
        dim1 = width * height
        base = _repeat(tf.range(batch_size) * dim1, out_height * out_width)
        base_y0 = base + y0 * dim2
        base_y1 = base + y1 * dim2
        idx_a = base_y0 + x0
        idx_b = base_y1 + x0
        idx_c = base_y0 + x1
        idx_d = base_y1 + x1

        # use indices to lookup pixels in the flat image and restore
        # channels dim
        im_flat = tf.reshape(im, tf.pack([-1, channels]))
        im_flat = tf.cast(im_flat, tf.float32)
        Ia = tf.gather(im_flat, idx_a)
        Ib = tf.gather(im_flat, idx_b)
        Ic = tf.gather(im_flat, idx_c)
        Id = tf.gather(im_flat, idx_d)

        # and finally calculate interpolated values
        x0_f = tf.cast(x0, tf.float32)
        x1_f = tf.cast(x1, tf.float32)
        y0_f = tf.cast(y0, tf.float32)
        y1_f = tf.cast(y1, tf.float32)
        wa = tf.expand_dims(((x1_f - x) * (y1_f - y)), 1)
        wb = tf.expand_dims(((x1_f - x) * (y - y0_f)), 1)
        wc = tf.expand_dims(((x - x0_f) * (y1_f - y)), 1)
        wd = tf.expand_dims(((x - x0_f) * (y - y0_f)), 1)
        output = tf.add_n([wa * Ia, wb * Ib, wc * Ic, wd * Id])
        return output


def _meshgrid(height, width):
    with tf.variable_scope('_meshgrid'):
        x_t = tf.matmul(tf.ones(shape=tf.pack([height, 1])), tf.transpose(
            tf.expand_dims(tf.linspace(-1.0, 1.0, width), 1), [1, 0]))
        y_t = tf.matmul(tf.expand_dims(
            tf.linspace(-1.0, 1.0, height), 1), tf.ones(shape=tf.pack([1, width])))

        x_t_flat = tf.reshape(x_t, (1, -1))
        y_t_flat = tf.reshape(y_t, (1, -1))

        ones = tf.ones_like(x_t_flat)
        grid = tf.concat(0, [x_t_flat, y_t_flat, ones])
        return grid


def subsample(inputs, factor, name=None):
    """Subsamples the input along the spatial dimensions.

    Args:
      inputs: A `Tensor` of size [batch, height_in, width_in, channels].
      factor: The subsampling factor.
      name: Optional variable_scope.

    Returns:
      output: A `Tensor` of size [batch, height_out, width_out, channels] with the
        input, either intact (if factor == 1) or subsampled (if factor > 1).
    """
    if factor == 1:
        return inputs
    else:
        return max_pool(inputs, filter_size=(1, 1), stride=(factor, factor), name=name)


def conv2d_same(inputs, num_outputs, kernel_size, stride, rate=1, name=None, **kwargs):
    """Strided 2-D convolution with 'SAME' padding.

    When stride > 1, then we do explicit zero-padding, followed by conv2d with
    'VALID' padding.

    Note that

       net = conv2d_same(inputs, num_outputs, 3, stride=stride)

    is equivalent to

       net = slim.conv2d(inputs, num_outputs, 3, stride=1, padding='SAME')
       net = subsample(net, factor=stride)

    whereas

       net = slim.conv2d(inputs, num_outputs, 3, stride=stride, padding='SAME')

    is different when the input's height or width is even, which is why we add the
    current function. For more details, see ResnetUtilsTest.testConv2DSameEven().

    Args:
      inputs: A 4-D tensor of size [batch, height_in, width_in, channels].
      num_outputs: An integer, the number of output filters.
      kernel_size: An int with the kernel_size of the filters.
      stride: An integer, the output stride.
      rate: An integer, rate for atrous convolution.
      name: name.

    Returns:
      output: A 4-D tensor of size [batch, height_out, width_out, channels] with
        the convolution output.
    """
    if stride == 1:
        return conv2d(inputs, num_outputs, filter_size=(kernel_size, kernel_size), stride=(1, 1), dilaton=rate,
                      padding='SAME', name=name, **kwargs)
    else:
        kernel_size_effective = kernel_size + (kernel_size - 1) * (rate - 1)
        pad_total = kernel_size_effective - 1
        pad_beg = pad_total // 2
        pad_end = pad_total - pad_beg
        inputs = tf.pad(inputs,
                        [[0, 0], [pad_beg, pad_end], [pad_beg, pad_end], [0, 0]])
        return conv2d(inputs, num_outputs, kernel_size, stride=stride,
                      dilation=rate, padding='VALID', name=name, **kwargs)


def bottleneck_v2(inputs, depth, depth_bottleneck, stride, rate=1, name=None, **kwargs):
    """Bottleneck residual unit variant with BN before convolutions.

    This is the full preactivation residual unit variant proposed in [2]. See
    Fig. 1(b) of [2] for its definition. Note that we use here the bottleneck
    variant which has an extra bottleneck layer.

    When putting together two consecutive ResNet blocks that use this unit, one
    should use stride = 2 in the last unit of the first block.

    Args:
      inputs: A tensor of size [batch, height, width, channels].
      depth: The depth of the ResNet unit output.
      depth_bottleneck: The depth of the bottleneck layers.
      stride: The ResNet unit's stride. Determines the amount of downsampling of
        the units output compared to its input.
      rate: An integer, rate for atrous convolution.
      outputs_collections: Collection to add the ResNet unit output.
      name: Optional variable_scope.

    Returns:
      The ResNet unit's output.
    """
    is_training = kwargs.get('is_training')
    reuse = kwargs.get('reuse')
    with tf.variable_scope(name, 'bottleneck_v2', [inputs]):
        depth_in = util.last_dimension(inputs.get_shape(), min_rank=4)
        preact = batch_norm(inputs, activation_fn=tf.nn.relu,
                            name='preact', is_training=is_training, reuse=reuse)
        if depth == depth_in:
            shortcut = subsample(inputs, stride, 'shortcut')
        else:
            shortcut = conv2d(preact, depth, is_training, reuse, filter_size=(1, 1), stride=(
                stride, stride), batch_norm=None, activation=None, name='shortcut')

        residual = conv2d(preact, depth_bottleneck, filter_size=(1, 1), stride=(1, 1),
                          name='conv1', **kwargs)
        residual = conv2d_same(residual, depth_bottleneck, 3, stride,
                               rate=rate, name='conv2', **kwargs)
        residual = conv2d(residual, depth, is_training, reuse, filter_size=(1, 1), stride=(1, 1),
                          batch_norm=None, activation=None, name='conv3')

        output = shortcut + residual

        return output


def bottleneck_v2(inputs, depth, depth_bottleneck, stride, rate=1, name=None, **kwargs):
    """Bottleneck residual unit variant with BN before convolutions.

    This is the full preactivation residual unit variant proposed in [2]. See
    Fig. 1(b) of [2] for its definition. Note that we use here the bottleneck
    variant which has an extra bottleneck layer.

    When putting together two consecutive ResNet blocks that use this unit, one
    should use stride = 2 in the last unit of the first block.

    Args:
      inputs: A tensor of size [batch, height, width, channels].
      depth: The depth of the ResNet unit output.
      depth_bottleneck: The depth of the bottleneck layers.
      stride: The ResNet unit's stride. Determines the amount of downsampling of
        the units output compared to its input.
      rate: An integer, rate for atrous convolution.
      outputs_collections: Collection to add the ResNet unit output.
      name: Optional variable_scope.

    Returns:
      The ResNet unit's output.
    """
    is_training = kwargs.get('is_training')
    reuse = kwargs.get('reuse')
    with tf.variable_scope(name, 'bottleneck_v2', [inputs]):
        depth_in = util.last_dimension(inputs.get_shape(), min_rank=4)
        preact = batch_norm(inputs, activation_fn=tf.nn.relu,
                            name='preact', is_training=is_training, reuse=reuse)
        if depth == depth_in:
            shortcut = subsample(inputs, stride, 'shortcut')
        else:
            shortcut = conv2d(preact, depth, is_training, reuse, filter_size=(1, 1), stride=(
                stride, stride), batch_norm=None, activation=None, name='shortcut')

        residual = conv2d(preact, depth_bottleneck, filter_size=(1, 1), stride=(1, 1),
                          name='conv1', **kwargs)
        residual = conv2d_same(residual, depth_bottleneck, 3, stride,
                               rate=rate, name='conv2', **kwargs)
        residual = conv2d(residual, depth, is_training, reuse, filter_size=(1, 1), stride=(1, 1),
                          batch_norm=None, activation=None, name='conv3')

        output = tf.nn.relu(shortcut + residual)

        return output


def memory_module(inputs, time, context, reuse, nwords, edim, mem_size, lindim, batch_size, regularizer=tf.nn.l2_loss, init_f=initz.random_normal(), trainable=True, **kwargs):
    with tf.variable_scope(name, reuse=reuse):
        global_step = tf.get_variable('global_step', shape=[
        ], dtype=tf.int64, initializer=tf.zeros_initializer, trainable=False)
        A_shape = B_shape = [nwords, edim]
        C_shape = [edim, edim]
        T_A_shape = T_B_shape = [mem_size, edim]
        A = tf.get_variable(
            name='A',
            shape=A_shape,
            initializer=init_f,
            regularizer=regularizer,
            trainable=trainable
        )
        B = tf.get_variable(
            name='B',
            shape=B_shape,
            initializer=init_f,
            regularizer=regularizer,
            trainable=trainable
        )
        C = tf.get_variable(
            name='C',
            shape=C_shape,
            initializer=init_f,
            regularizer=regularizer,
            trainable=trainable
        )
        T_A = tf.get_variable(
            name='T_A',
            shape=T_A_shape,
            initializer=init_f,
            regularizer=regularizer,
            trainable=trainable
        )
        T_B = tf.get_variable(
            name='T_B',
            shape=T_B_shape,
            initializer=init_f,
            regularizer=regularizer,
            trainable=trainable
        )

        # m_i = sum A_ij * x_ij + T_A_i
        Ain_c = tf.nn.embedding_lookup(A, context)
        Ain_t = tf.nn.embedding_lookup(T_A, time)
        Ain = tf.add(Ain_c, Ain_t)

        # c_i = sum B_ij * u + T_B_i
        Bin_c = tf.nn.embedding_lookup(B, context)
        Bin_t = tf.nn.embedding_lookup(T_B, time)
        Bin = tf.add(Bin_c, Bin_t)

        hid = []
        hid.append(inputs)

        for h in xrange(nhop):
            hid3dim = tf.reshape(hid[-1], [-1, 1, edim])
            Aout = tf.batch_matmul(hid3dim, Ain, adj_y=True)
            Aout2dim = tf.reshape(Aout, [-1, mem_size])
            P = tf.nn.softmax(Aout2dim)

            probs3dim = tf.reshape(P, [-1, 1, mem_size])
            Bout = tf.batch_matmul(probs3dim, Bin)
            Bout2dim = tf.reshape(Bout, [-1, edim])

            Cout = tf.matmul(hid[-1], C)
            Dout = tf.add(Cout, Bout2dim)

            self.share_list[0].append(Cout)

            if lindim == edim:
                hid.append(Dout)
            elif lindim == 0:
                hid.append(tf.nn.relu(Dout))
            else:
                F = tf.slice(Dout, [0, 0], [batch_size, lindim])
                G = tf.slice(Dout, [0, lindim], [batch_size, edim - lindim])
                K = tf.nn.relu(G)
                hid.append(tf.concat(1, [F, K]))
        return hid
