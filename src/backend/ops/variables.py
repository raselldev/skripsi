from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import enum  # pylint: disable=g-bad-import-order

import six

from backend.protobuf import attr_value_pb2
from backend import context
from backend.ops import state_ops
from backend.ops import array_ops
from backend.framework import ops
from backend.training import base as checkpointable
from backend.util import compat
#from backend.python.util import tf_should_use
#from backend.python.util.deprecation import deprecated

class VariableSynchronization(enum.Enum):
  AUTO = 0
  NONE = 1
  ON_WRITE = 2
  ON_READ = 3

class VariableAggregation(enum.Enum):
  NONE = 0
  SUM = 1
  MEAN = 2
  ONLY_FIRST_TOWER = 3

class VariableMetaclass(type):

  def _variable_v1_call(cls,
                        initial_value=None,
                        trainable=None,
                        collections=None,
                        validate_shape=True,
                        caching_device=None,
                        name=None,
                        variable_def=None,
                        dtype=None,
                        expected_shape=None,
                        import_scope=None,
                        constraint=None,
                        use_resource=None,
                        synchronization=VariableSynchronization.AUTO,
                        aggregation=VariableAggregation.NONE):
    previous_getter = lambda **kwargs: default_variable_creator(None, **kwargs)
    for getter in ops.get_default_graph()._variable_creator_stack:  # pylint: disable=protected-access
      previous_getter = _make_getter(getter, previous_getter)

    # Reset `aggregation` that is explicitly set as `None` to the enum NONE.
    if aggregation is None:
      aggregation = VariableAggregation.NONE
    return previous_getter(
        initial_value=initial_value,
        trainable=trainable,
        collections=collections,
        validate_shape=validate_shape,
        caching_device=caching_device,
        name=name,
        variable_def=variable_def,
        dtype=dtype,
        expected_shape=expected_shape,
        import_scope=import_scope,
        constraint=constraint,
        use_resource=use_resource,
        synchronization=synchronization,
        aggregation=aggregation)

  def _variable_v2_call(cls,
                        initial_value=None,
                        trainable=None,
                        validate_shape=True,
                        caching_device=None,
                        name=None,
                        variable_def=None,
                        dtype=None,
                        import_scope=None,
                        constraint=None,
                        synchronization=VariableSynchronization.AUTO,
                        aggregation=VariableAggregation.NONE):
    previous_getter = lambda **kws: default_variable_creator_v2(None, **kws)
    for getter in ops.get_default_graph()._variable_creator_stack:  # pylint: disable=protected-access
      previous_getter = _make_getter(getter, previous_getter)

    # Reset `aggregation` that is explicitly set as `None` to the enum NONE.
    if aggregation is None:
      aggregation = VariableAggregation.NONE
    return previous_getter(
        initial_value=initial_value,
        trainable=trainable,
        validate_shape=validate_shape,
        caching_device=caching_device,
        name=name,
        variable_def=variable_def,
        dtype=dtype,
        import_scope=import_scope,
        constraint=constraint,
        synchronization=synchronization,
        aggregation=aggregation)

  def __call__(cls, *args, **kwargs):
    if cls is VariableV1:
      return cls._variable_v1_call(*args, **kwargs)
    elif cls is Variable:
      return cls._variable_v2_call(*args, **kwargs)
    else:
      return super(VariableMetaclass, cls).__call__(*args, **kwargs)

class Variable(six.with_metaclass(VariableMetaclass,
                                  checkpointable.CheckpointableBase)):

  def __init__(self,
               initial_value=None,
               trainable=True,
               validate_shape=True,
               caching_device=None,
               name=None,
               variable_def=None,
               dtype=None,
               import_scope=None,
               constraint=None,
               synchronization=VariableSynchronization.AUTO,
               aggregation=VariableAggregation.NONE):
    raise NotImplementedError




  # Conversion to tensor.
  @staticmethod
  def _TensorConversionFunction(v, dtype=None, name=None, as_ref=False):  # pylint: disable=invalid-name
    _ = name
    if dtype and not dtype.is_compatible_with(v.dtype):
      raise ValueError(
          "Incompatible type conversion requested to type '%s' for variable "
          "of type '%s'" % (dtype.name, v.dtype.name))
    if as_ref:
      return v._ref()  # pylint: disable=protected-access
    else:
      return v.value()

  @staticmethod
  def _OverloadAllOperators():  # pylint: disable=invalid-name
    for operator in ops.Tensor.OVERLOADABLE_OPERATORS:
      Variable._OverloadOperator(operator)
    # For slicing, bind getitem differently than a tensor (use SliceHelperVar
    # instead)
    # pylint: disable=protected-access
    setattr(Variable, "__getitem__", array_ops._SliceHelperVar)

  @staticmethod
  def _OverloadOperator(operator):  # pylint: disable=invalid-name

    def _run_op(a, *args):
      # pylint: disable=protected-access
      return getattr(ops.Tensor, operator)(a._AsTensor(), *args)
    # Propagate __doc__ to wrapper
    try:
      _run_op.__doc__ = getattr(ops.Tensor, operator).__doc__
    except AttributeError:
      pass

    setattr(Variable, operator, _run_op)


  __array_priority__ = 100


  @staticmethod
  def from_proto(variable_def, import_scope=None):
    return RefVariable(variable_def=variable_def,
                       import_scope=import_scope)

  class SaveSliceInfo(object):
    def __init__(self,
                 full_name=None,
                 full_shape=None,
                 var_offset=None,
                 var_shape=None,
                 save_slice_info_def=None,
                 import_scope=None):
      if save_slice_info_def:
        assert isinstance(save_slice_info_def, variable_pb2.SaveSliceInfoDef)
        self.full_name = ops.prepend_name_scope(
            save_slice_info_def.full_name, import_scope=import_scope)
        self.full_shape = [i for i in save_slice_info_def.full_shape]
        self.var_offset = [i for i in save_slice_info_def.var_offset]
        self.var_shape = [i for i in save_slice_info_def.var_shape]
      else:
        self.full_name = full_name
        self.full_shape = full_shape
        self.var_offset = var_offset
        self.var_shape = var_shape

    @property
    def spec(self):
      full_shape_str = " ".join(["%d" % d for d in self.full_shape]) + " "
      sl_spec = ":".join([
          "%d,%d" % (o, s) for o, s in zip(self.var_offset, self.var_shape)
      ])
      return full_shape_str + sl_spec

    def to_proto(self, export_scope=None):
      if (export_scope is None or
          self.full_name.startswith(export_scope)):
        save_slice_info_def = variable_pb2.SaveSliceInfoDef()
        save_slice_info_def.full_name = ops.strip_name_scope(
            self.full_name, export_scope)
        for i in self.full_shape:
          save_slice_info_def.full_shape.append(i)
        for i in self.var_offset:
          save_slice_info_def.var_offset.append(i)
        for i in self.var_shape:
          save_slice_info_def.var_shape.append(i)
        return save_slice_info_def
      else:
        return None

  

class VariableV1(Variable):
  def __init__(self,  # pylint: disable=super-init-not-called
               initial_value=None,
               trainable=True,
               collections=None,
               validate_shape=True,
               caching_device=None,
               name=None,
               variable_def=None,
               dtype=None,
               expected_shape=None,
               import_scope=None,
               constraint=None,
               use_resource=None,
               synchronization=VariableSynchronization.AUTO,
               aggregation=VariableAggregation.NONE):
               SaveSliceInfo = Variable.SaveSliceInfo

class RefVariable(VariableV1):

  def __init__(self,  # pylint: disable=super-init-not-called
               initial_value=None,
               trainable=True,
               collections=None,
               validate_shape=True,
               caching_device=None,
               name=None,
               variable_def=None,
               dtype=None,
               expected_shape=None,
               import_scope=None,
               constraint=None):
    self._in_graph_mode = True
    if variable_def:
      # If variable_def is provided, recreates the variable from its fields.
      if initial_value:
        raise ValueError("variable_def and initial_value are mutually "
                         "exclusive.")
      self._init_from_proto(variable_def, import_scope=import_scope)
    else:
      # Create from initial_value.
      self._init_from_args(
          initial_value=initial_value,
          trainable=trainable,
          collections=collections,
          validate_shape=validate_shape,
          caching_device=caching_device,
          name=name,
          dtype=dtype,
          expected_shape=expected_shape,
          constraint=constraint)

  def __repr__(self):
    if context.executing_eagerly() and not self._in_graph_mode:
      return "<tf.Variable '%s' shape=%s dtype=%s, numpy=%s>" % (
          self.name, self.get_shape(), self.dtype.name,
          ops.numpy_text(self.read_value(), is_repr=True))
    else:
      return "<tf.Variable '%s' shape=%s dtype=%s>" % (
          self.name, self.get_shape(), self.dtype.name)

  def _init_from_args(self,
                      initial_value=None,
                      trainable=True,
                      collections=None,
                      validate_shape=True,
                      caching_device=None,
                      name=None,
                      dtype=None,
                      expected_shape=None,
                      constraint=None):
    _ = expected_shape
    if initial_value is None:
      raise ValueError("initial_value must be specified.")
    init_from_fn = callable(initial_value)

    if collections is None:
      collections = [ops.GraphKeys.GLOBAL_VARIABLES]
    if not isinstance(collections, (list, tuple, set)):
      raise ValueError(
          "collections argument to Variable constructor must be a list, tuple, "
          "or set. Got %s of type %s" % (collections, type(collections)))
    if constraint is not None and not callable(constraint):
      raise ValueError("The `constraint` argument must be a callable.")

    # Store the graph key so optimizers know how to only retrieve variables from
    # this graph.
    self._graph_key = ops.get_default_graph()._graph_key  # pylint: disable=protected-access
    if isinstance(initial_value, checkpointable.CheckpointInitialValue):
      self._maybe_initialize_checkpointable()
      self._update_uid = initial_value.checkpoint_position.restore_uid
      initial_value = initial_value.wrapped_value

    self._trainable = trainable
    if trainable and ops.GraphKeys.TRAINABLE_VARIABLES not in collections:
      collections = list(collections) + [ops.GraphKeys.TRAINABLE_VARIABLES]
    with ops.init_scope():
      # Ensure that we weren't lifted into the eager context.
      if context.executing_eagerly():
        raise RuntimeError(
            "RefVariable not supported when eager execution is enabled. ")
      with ops.name_scope(name, "Variable", [] if init_from_fn else
                          [initial_value]) as name:

        if init_from_fn:
          # Use attr_scope and device(None) to simulate the behavior of
          # colocate_with when the variable we want to colocate with doesn't
          # yet exist.
          true_name = ops._name_from_scope_name(name)  # pylint: disable=protected-access
          attr = attr_value_pb2.AttrValue(
              list=attr_value_pb2.AttrValue.ListValue(
                  s=[compat.as_bytes("loc:@%s" % true_name)]))
          # pylint: disable=protected-access
          with ops.get_default_graph()._attr_scope({"_class": attr}):
            with ops.name_scope("Initializer"), ops.device(None):
              self._initial_value = ops.convert_to_tensor(
                  initial_value(), name="initial_value", dtype=dtype)
              shape = (self._initial_value.get_shape()
                       if validate_shape else tensor_shape.unknown_shape())
            self._variable = state_ops.variable_v2(
                shape,
                self._initial_value.dtype.base_dtype,
                name=name)
          # pylint: enable=protected-access

        # Or get the initial value from a Tensor or Python object.
        else:
          self._initial_value = ops.convert_to_tensor(
              initial_value, name="initial_value", dtype=dtype)
          # pylint: disable=protected-access
          if self._initial_value.op._get_control_flow_context() is not None:
            raise ValueError(
                "Initializer for variable %s is from inside a control-flow "
                "construct, such as a loop or conditional. When creating a "
                "variable inside a loop or conditional, use a lambda as the "
                "initializer." % name)
          # pylint: enable=protected-access
          shape = (self._initial_value.get_shape()
                   if validate_shape else tensor_shape.unknown_shape())
          # In this case, the variable op can't be created until after the
          # initial_value has been converted to a Tensor with a known type.
          self._variable = state_ops.variable_v2(
              shape,
              self._initial_value.dtype.base_dtype,
              name=name)

        # Manually overrides the variable's shape with the initial value's.
        if validate_shape:
          initial_value_shape = self._initial_value.get_shape()
          if not initial_value_shape.is_fully_defined():
            raise ValueError("initial_value must have a shape specified: %s" %
                             self._initial_value)

        # If 'initial_value' makes use of other variables, make sure we don't
        # have an issue if these other variables aren't initialized first by
        # using their initialized_value() method.
        self._initializer_op = state_ops.assign(
            self._variable,
            self._try_guard_against_uninitialized_dependencies(
                self._initial_value),
            validate_shape=validate_shape).op

        # TODO(vrv): Change this class to not take caching_device, but
        # to take the op to colocate the snapshot with, so we can use
        # colocation rather than devices.
        if caching_device is not None:
          with ops.device(caching_device):
            self._snapshot = array_ops.identity(self._variable, name="read")
        else:
          with ops.colocate_with(self._variable.op):
            self._snapshot = array_ops.identity(self._variable, name="read")
      ops.add_to_collections(collections, self)

    self._caching_device = caching_device
    self._save_slice_info = None
    self._constraint = constraint

  def _init_from_proto(self, variable_def, import_scope=None):
    assert isinstance(variable_def, variable_pb2.VariableDef)
    # Create from variable_def.
    g = ops.get_default_graph()
    self._variable = g.as_graph_element(
        ops.prepend_name_scope(variable_def.variable_name,
                               import_scope=import_scope))
    self._initializer_op = g.as_graph_element(
        ops.prepend_name_scope(variable_def.initializer_name,
                               import_scope=import_scope))
    # Tests whether initial_value_name exists first for backwards compatibility.
    if (hasattr(variable_def, "initial_value_name") and
        variable_def.initial_value_name):
      self._initial_value = g.as_graph_element(
          ops.prepend_name_scope(variable_def.initial_value_name,
                                 import_scope=import_scope))
    else:
      self._initial_value = None
    self._trainable = getattr(variable_def, "trainable", True)
    self._snapshot = g.as_graph_element(
        ops.prepend_name_scope(variable_def.snapshot_name,
                               import_scope=import_scope))
    if variable_def.HasField("save_slice_info_def"):
      self._save_slice_info = Variable.SaveSliceInfo(
          save_slice_info_def=variable_def.save_slice_info_def,
          import_scope=import_scope)
    else:
      self._save_slice_info = None
    self._caching_device = None
    self._constraint = None

  def _as_graph_element(self):
    return self._variable

  def _AsTensor(self):  # pylint: disable=invalid-name
    return self._snapshot

  def __iter__(self):
    raise TypeError("'Variable' object is not iterable.")

  def value(self):
    return self._snapshot

  def read_value(self):
    return array_ops.identity(self._variable, name="read")

  def _ref(self):
    return self._variable

  def set_shape(self, shape):
    self._ref().set_shape(shape)
    self.value().set_shape(shape)

  @property
  def trainable(self):
    return self._trainable

  def eval(self, session=None):
    return self._variable.eval(session=session)

  def initialized_value(self):
    with ops.init_scope():
      return control_flow_ops.cond(is_variable_initialized(self),
                                   self.read_value,
                                   lambda: self.initial_value)

  @property
  def initial_value(self):
    return self._initial_value

  @property
  def constraint(self):
    return self._constraint

  def assign(self, value, use_locking=False, name=None, read_value=True):
    assign = state_ops.assign(self._variable, value, use_locking=use_locking,
                              name=name)
    if read_value:
      return assign
    return assign.op

  def assign_add(self, delta, use_locking=False, name=None, read_value=True):
    assign = state_ops.assign_add(
        self._variable, delta, use_locking=use_locking, name=name)
    if read_value:
      return assign
    return assign.op

  def assign_sub(self, delta, use_locking=False, name=None, read_value=True):
    assign = state_ops.assign_sub(
        self._variable, delta, use_locking=use_locking, name=name)
    if read_value:
      return assign
    return assign.op

  def scatter_sub(self, sparse_delta, use_locking=False, name=None):
    if not isinstance(sparse_delta, ops.IndexedSlices):
      raise ValueError("sparse_delta is not IndexedSlices: %s" % sparse_delta)
    return gen_state_ops.scatter_sub(
        self._variable,
        sparse_delta.indices,
        sparse_delta.values,
        use_locking=use_locking,
        name=name)

  def scatter_add(self, sparse_delta, use_locking=False, name=None):
    if not isinstance(sparse_delta, ops.IndexedSlices):
      raise ValueError("sparse_delta is not IndexedSlices: %s" % sparse_delta)
    return gen_state_ops.scatter_add(
        self._variable,
        sparse_delta.indices,
        sparse_delta.values,
        use_locking=use_locking,
        name=name)

  def scatter_update(self, sparse_delta, use_locking=False, name=None):
    if not isinstance(sparse_delta, ops.IndexedSlices):
      raise ValueError("sparse_delta is not IndexedSlices: %s" % sparse_delta)
    return gen_state_ops.scatter_update(
        self._variable,
        sparse_delta.indices,
        sparse_delta.values,
        use_locking=use_locking,
        name=name)

  def scatter_nd_sub(self, indices, updates, name=None):
    return gen_state_ops.scatter_nd_sub(
        self._variable, indices, updates, use_locking=True, name=name)

  def scatter_nd_add(self, indices, updates, name=None):
    return gen_state_ops.scatter_nd_add(
        self._variable, indices, updates, use_locking=True, name=name)

  def scatter_nd_update(self, indices, updates, name=None):
    return gen_state_ops.scatter_nd_update(
        self._variable, indices, updates, use_locking=True, name=name)

  def _strided_slice_assign(self,
                            begin,
                            end,
                            strides,
                            value,
                            name,
                            begin_mask,
                            end_mask,
                            ellipsis_mask,
                            new_axis_mask,
                            shrink_axis_mask):
    return gen_array_ops.strided_slice_assign(ref=self._ref(),
                                              begin=begin,
                                              end=end,
                                              strides=strides,
                                              value=value,
                                              name=name,
                                              begin_mask=begin_mask,
                                              end_mask=end_mask,
                                              ellipsis_mask=ellipsis_mask,
                                              new_axis_mask=new_axis_mask,
                                              shrink_axis_mask=shrink_axis_mask)

  def count_up_to(self, limit):
    return state_ops.count_up_to(self._variable, limit=limit)

  def load(self, value, session=None):
    if context.executing_eagerly():
      self.assign(value)
    else:
      session = session or ops.get_default_session()
      if session is None:
        raise ValueError(
            "Either session argument should be provided or default session "
            "should be established")
      session.run(self._initializer_op, {self._initializer_op.inputs[1]: value})

  # Conversion to tensor.
  @staticmethod
  def _TensorConversionFunction(v, dtype=None, name=None, as_ref=False):  # pylint: disable=invalid-name
    _ = name
    if dtype and not dtype.is_compatible_with(v.dtype):
      raise ValueError(
          "Incompatible type conversion requested to type '%s' for variable "
          "of type '%s'" % (dtype.name, v.dtype.name))
    if as_ref:
      return v._ref()  # pylint: disable=protected-access
    else:
      return v.value()

  @staticmethod
  def _OverloadAllOperators():  # pylint: disable=invalid-name
    for operator in ops.Tensor.OVERLOADABLE_OPERATORS:
      Variable._OverloadOperator(operator)  # pylint: disable=protected-access
    # For slicing, bind getitem differently than a tensor (use SliceHelperVar
    # instead)
    # pylint: disable=protected-access
    setattr(Variable, "__getitem__", array_ops._SliceHelperVar)

  @staticmethod
  def _OverloadOperator(operator):  # pylint: disable=invalid-name
    def _run_op(a, *args):
      # pylint: disable=protected-access
      return getattr(ops.Tensor, operator)(a._AsTensor(), *args)
    # Propagate __doc__ to wrapper
    try:
      _run_op.__doc__ = getattr(ops.Tensor, operator).__doc__
    except AttributeError:
      pass

    setattr(Variable, operator, _run_op)

  def _gather_saveables_for_checkpoint(self):
    return {checkpointable.VARIABLE_VALUE_KEY: self}

  def _try_guard_against_uninitialized_dependencies(self, initial_value):
    if not isinstance(initial_value, ops.Tensor):
      raise TypeError("initial_value needs to be a Tensor: %s" % initial_value)

    # Don't modify initial_value if it contains any cyclic dependencies.
    def has_cycle(op, path):
      if op.name in path:
        return True
      path.add(op.name)
      for op_input in op.inputs:
        if has_cycle(op_input.op, path):
          return True
      for op_control_input in op.control_inputs:
        if has_cycle(op_control_input, path):
          return True
      path.remove(op.name)
      return False
    if has_cycle(initial_value.op, path=set()):
      return initial_value

    return self._safe_initial_value_from_tensor(initial_value, op_cache={})

  def _safe_initial_value_from_tensor(self, tensor, op_cache):
    op = tensor.op
    new_op = op_cache.get(op.name)
    if new_op is None:
      new_op = self._safe_initial_value_from_op(op, op_cache)
      op_cache[op.name] = new_op
    return new_op.outputs[tensor.value_index]

  def _safe_initial_value_from_op(self, op, op_cache):
    op_type = op.node_def.op
    if op_type in ("IsVariableInitialized", "VarIsInitializedOp",
                   "ReadVariableOp"):
      return op

    # Attempt to find the initialized_value of any variable reference / handles.
    # TODO(b/70206927): Fix handling of ResourceVariables.
    if op_type in ("Variable", "VariableV2", "VarHandleOp"):
      initialized_value = self._find_initialized_value_for_variable(op)
      return op if initialized_value is None else initialized_value.op

    # Recursively build initializer expressions for inputs.
    modified = False
    new_op_inputs = []
    for op_input in op.inputs:
      new_op_input = self._safe_initial_value_from_tensor(op_input, op_cache)
      new_op_inputs.append(new_op_input)
      modified = modified or (new_op_input != op_input)

    # If at least one input was modified, replace the op.
    if modified:
      new_op_type = op_type
      if new_op_type == "RefSwitch":
        new_op_type = "Switch"
      new_op_name = op.node_def.name + "_" + self.name
      new_op_name = new_op_name.replace(":", "_")
      return self.graph.create_op(
          new_op_type, new_op_inputs,
          op._output_types,  # pylint: disable=protected-access
          name=new_op_name, attrs=op.node_def.attr)

    return op

  def _find_initialized_value_for_variable(self, variable_op):
    try:
      var_names = [variable_op.node_def.name, variable_op.node_def.name + ":0"]
      for collection_name in (ops.GraphKeys.GLOBAL_VARIABLES,
                              ops.GraphKeys.LOCAL_VARIABLES):
        for var in self.graph.get_collection(collection_name):
          if var.name in var_names:
            return var.initialized_value()
    except AttributeError:
      # Return None when an incomplete user-defined variable type was put in
      # the collection.
      return None
    return None

  # NOTE(mrry): This enables the Variable's overloaded "right" binary
  # operators to run when the left operand is an ndarray, because it
  # accords the Variable class higher priority than an ndarray, or a
  # numpy matrix.
  # TODO(mrry): Convert this to using numpy's __numpy_ufunc__
  # mechanism, which allows more control over how Variables interact
  # with ndarrays.
  __array_priority__ = 100

  @property
  def name(self):
    return self._variable.name

  @property
  def _shared_name(self):
    return self.name[:-2]

  @property
  def initializer(self):
    return self._initializer_op

  @property
  def device(self):
    return self._variable.device

  @property
  def dtype(self):
    return self._variable.dtype

  @property
  def op(self):
    return self._variable.op

  @property
  def graph(self):
    return self._variable.graph

  @property
  def shape(self):
    return self._variable.get_shape()

  def get_shape(self):
    return self.shape

  def to_proto(self, export_scope=None):
    if (export_scope is None or
        self._variable.name.startswith(export_scope)):
      var_def = variable_pb2.VariableDef()
      var_def.variable_name = ops.strip_name_scope(
          self._variable.name, export_scope)
      if self._initial_value is not None:
        # For backwards compatibility.
        var_def.initial_value_name = ops.strip_name_scope(
            self._initial_value.name, export_scope)
      var_def.trainable = self.trainable
      var_def.initializer_name = ops.strip_name_scope(
          self.initializer.name, export_scope)
      var_def.snapshot_name = ops.strip_name_scope(
          self._snapshot.name, export_scope)
      if self._save_slice_info:
        var_def.save_slice_info_def.MergeFrom(self._save_slice_info.to_proto(
            export_scope=export_scope))
      return var_def
    else:
      return None

  def _set_save_slice_info(self, save_slice_info):
    self._save_slice_info = save_slice_info

  def _get_save_slice_info(self):
    return self._save_slice_info

def trainable_variables(scope=None):
  return ops.get_collection(ops.GraphKeys.TRAINABLE_VARIABLES, scope)

def _all_saveable_objects(scope=None):
  return (ops.get_collection(ops.GraphKeys.GLOBAL_VARIABLES, scope) +
          ops.get_collection(ops.GraphKeys.SAVEABLE_OBJECTS, scope))

