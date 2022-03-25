from re import L
import sys
from typing import Dict, Optional

import numpy as np
import tensorflow as tf

from trulens.nn.backend import get_backend
from trulens.nn.models._model_base import ModelWrapper
from trulens.nn.quantities import QoI
from trulens.nn.slices import Cut
from trulens.nn.slices import InputCut
from trulens.nn.slices import LogitCut
from trulens.nn.slices import OutputCut
from trulens.utils import tru_logger
from trulens.utils.typing import ArgsLike
from trulens.utils.typing import as_inputs
from trulens.utils.typing import DATA_CONTAINER_TYPE
from trulens.utils.typing import InterventionLike
from trulens.utils.typing import KwargsLike
from trulens.utils.typing import ModelInputs


class TensorflowModelWrapper(ModelWrapper):
    """
    Model wrapper that exposes the internal components
    of Tensorflow objects.
    """

    def __init__(
        self,
        graph,
        input_tensors,
        output_tensors,
        internal_tensor_dict=None,
        session=None
    ):
        """
        Parameters
        ----------
        graph : tf.Graph
            The computation graph representing the model.
        input_tensors : Tensor | list of Tensor
            A list of the tensors that are the inputs to the graph. If there is
            only one input, it can be given without wrapping it in a list.
            This is needed as the input tensors of a graph cannot be inferred.
        output_tensors : Tensor | list of Tensor
            A list of the tensors that are the outputs to the graph. If there is
            only one output, it can be given without wrapping it in a list.
            This is needed as the output tensors of a graph cannot be inferred.
        internal_tensor_dict : dict of str -> tensor, optional
            The user can specify their own accessors to the tensors they would
            like to expose in the graph by passing a dict that maps their chosen
            name to the corresponding tensor. Any tensors not given in
            `internal_tensor_dict` can be accessed via the name given to them by
            tensorflow.
        """
        if input_tensors is None:
            raise ValueError(
                'Tensorflow1 model wrapper must pass the input_tensors parameter'
            )
        if output_tensors is None:
            raise ValueError(
                'Tensorflow1 model wrapper must pass the output_tensors parameter'
            )

        self._graph = graph

        self._inputs = (
            input_tensors if isinstance(input_tensors, DATA_CONTAINER_TYPE) else
            [input_tensors]
        )
        self._outputs = (
            output_tensors if isinstance(output_tensors, DATA_CONTAINER_TYPE)
            else [output_tensors]
        )

        self._internal_tensors = (
            internal_tensor_dict if internal_tensor_dict is not None else {}
        )

        self._session = session

        # This cache will be used to not recreate gradient nodes if they have
        # already been created.
        self._cached_gradient_tensors = {}

    def _get_layer(self, name):
        if name in self._internal_tensors:
            return self._internal_tensors[name]
        else:
            try:
                return self._graph.get_tensor_by_name(name)

            except KeyError:
                raise ValueError('No such layer tensor:', name)

    def _get_layers(self, cut):
        if isinstance(cut, InputCut):
            return self._inputs

        elif isinstance(cut, OutputCut):
            return self._outputs

        elif isinstance(cut, LogitCut):
            if 'logits' in self._internal_tensors:
                layers = self._internal_tensors['logits']

            else:
                raise ValueError(
                    '`LogitCut` was used, but the model has not specified the '
                    'tensors that correspond to the logit output.'
                )

        elif isinstance(cut.name, DATA_CONTAINER_TYPE):
            layers = [self._get_layer(name) for name in cut.name]

        else:
            layers = self._get_layer(cut.name)

        return layers if isinstance(layers, DATA_CONTAINER_TYPE) else [layers]

    def _prepare_feed_dict_with_intervention(
        self, model_args, model_kwargs, intervention, doi_tensors
    ):

        B = get_backend()

        feed_dict = {}
        input_tensors = model_args

        if isinstance(
                input_tensors,
                DATA_CONTAINER_TYPE) and len(input_tensors) > 0 and isinstance(
                    input_tensors[0], DATA_CONTAINER_TYPE):
            input_tensors = input_tensors[0]

        num_args = len(input_tensors)
        num_kwargs = len(model_kwargs)
        num_expected = len(self._inputs)

        if num_args + num_kwargs != num_expected:
            raise ValueError(
                "Expected to get {num_expected} inputs but got {num_args} from args and {num_kwargs} from kwargs."
            )

        if num_args > 0 and num_kwargs > 0:
            tru_logger.warn(
                "Got both args and kwargs as inputs; we assume the args correspond to the first input tensors."
            )

        # set the first few tensors from args
        feed_dict.update(
            {
                input_tensor: xi for input_tensor, xi in
                zip(self._inputs[0:num_args], input_tensors)
            }
        )

        def _tensor(k):
            if tf.is_tensor(k):
                return k
            elif k in self._internal_tensors:
                return self._internal_tensors[k]
            else:
                raise ValueError(f"do not know how to map {k} to a tensor")

        # and the reset from kwargs
        if model_kwargs is not None:
            feed_dict.update({_tensor(k): v for k, v in model_kwargs.items()})

        # Keep track which feed tensors came from intervention (as opposed to model inputs) so that
        # ones from model inputs that were not overriden by intervention can be tiled.
        intervention_dict = dict()

        # Convert `intervention` to a list of inputs if it isn't already.
        if intervention is not None:
            if isinstance(intervention, dict):
                args = []
                kwargs = intervention

            elif isinstance(intervention, ModelInputs):
                args = intervention.args
                kwargs = intervention.kwargs

            else:
                args = as_inputs(intervention)
                kwargs = {}

            # TODO: Figure out a way to run the check below for InputCut. It currently
            # does not work for those cuts.
            #if len(args) + len(kwargs) != len(doi_tensors):
            #    raise ValueError(f"Expected to get {len(doi_tensors)} inputs for intervention but got {len(args)} args and {len(kwargs)} kwargs.")

            intervention_dict.update(
                {k: v for k, v in zip(doi_tensors[0:len(args)], args)}
            )
            intervention_dict.update({_tensor(k): v for k, v in kwargs.items()})
            feed_dict.update(intervention_dict)

            intervention = list(args) + [feed_dict[_tensor(k)] for k in kwargs]

            doi_repeated_batch_size = intervention[0].shape[0]
            expected_dim = None

            # tile the feed tensors that came from model input arguments
            # TODO(piotrm): figure out how to abstract this out from the backends
            for k, val in feed_dict.items():
                if k in intervention_dict:
                    continue

                if isinstance(val, np.ndarray):
                    doi_resolution = int(doi_repeated_batch_size / val.shape[0])
                    if expected_dim is None:
                        expected_dim = val.shape[0]

                    if expected_dim == val.shape[0]:
                        tile_shape = [1] * len(val.shape)
                        tile_shape[0] = doi_resolution
                        feed_dict[k] = np.tile(val, tuple(tile_shape))
                    else:
                        tru_logger.warn(
                            f"Value {val} of shape {val.shape} is assumed to not be batchable due to its shape not matching prior batchable inputs of shape ({expected_dim}, ...). If this is incorrect, make sure its first dimension matches prior batchable inputs."
                        )

        elif intervention is None and doi_tensors == self._inputs:
            intervention = [feed_dict[key_tensor] for key_tensor in doi_tensors]

        else:
            # this might no longer be possible given the size checks earlier in the method
            intervention = []

        return feed_dict, intervention

    def _fprop(
        self,
        *,
        model_inputs: ModelInputs,
        doi_cut: Cut,
        to_cut: Cut,
        attribution_cut: Cut,
        intervention: ModelInputs
    ):
        """
        See ModelWrapper.fprop .
        """

        model_args = model_inputs.args
        model_kwargs = model_inputs.kwargs
        intervention = intervention.args

        doi_tensors = self._get_layers(doi_cut)
        to_tensors = self._get_layers(to_cut)

        feed_dict, intervention = self._prepare_feed_dict_with_intervention(
            model_args, model_kwargs, intervention, doi_tensors
        )

        # Tensorlow doesn't allow you to make a function that returns the same
        # tensor as it takes in. Thus, we have to have a special case for the
        # identity function. Any tensors that are both in `doi_tensors` and
        # `to_tensors` cannot be computed via a `keras.backend.function` and
        # thus need to be taken from the input, `x`.
        identity_map = {
            i: j for i, to_tensor in enumerate(to_tensors)
            for j, from_tensor in enumerate(doi_tensors)
            if to_tensor == from_tensor
        }

        non_identity_to_tensors = [
            to_tensor for i, to_tensor in enumerate(to_tensors)
            if i not in identity_map
        ]

        # Compute the output values of `to_tensors` unless all `to_tensor`s were
        # also `doi_tensors`.
        if non_identity_to_tensors:
            out_vals = self._run_session(non_identity_to_tensors, feed_dict)

        else:
            out_vals = []

        # For any `to_tensor`s that were also `from_tensor`s, insert the
        # corresponding concrete input value from `x` in the output's place.
        for i in sorted(identity_map):
            out_vals.insert(i, intervention[identity_map[i]])

        return out_vals

    def _run_session(self, outs, feed_dict):
        B = get_backend()
        
        feed_dict = {k: B.as_array(v) if B.is_tensor(v) else v for k, v in feed_dict.items()}

        if self._session is not None:
            return self._session.run(outs, feed_dict=feed_dict)

        else:
            with tf.Session(graph=self._graph) as session:
                try:
                    return session.run(outs, feed_dict=feed_dict)
                except tf.errors.FailedPreconditionError:
                    tb = sys.exc_info()[2]
                    raise RuntimeError(
                        'Encountered uninitialized session variables. This could be caused by not saving all variables, or from other tensorflow default session implementation issues. Try passing in the session to the ModelWrapper __init__ function.'
                    ).with_traceback(tb)

    def _qoi_bprop(
        self,
        *,
        qoi: QoI,
        model_inputs: ModelInputs,
        doi_cut: Cut,
        to_cut: Cut,
        attribution_cut: Cut,
        intervention: ModelInputs
    ):
        """
        See ModelWrapper.qoi_bprop .
        """

        model_args = model_inputs.args
        model_kwargs = model_inputs.kwargs

        attribution_tensors = self._get_layers(attribution_cut)
        to_tensors = self._get_layers(to_cut)
        doi_tensors = self._get_layers(doi_cut)

        feed_dict, _ = self._prepare_feed_dict_with_intervention(
            model_args, model_kwargs, intervention, doi_tensors
        )
        z_grads = []
        with self._graph.as_default():
            for z in attribution_tensors:
                gradient_tensor_key = (z, frozenset(to_tensors))
                if gradient_tensor_key in self._cached_gradient_tensors:
                    grads = self._cached_gradient_tensors[gradient_tensor_key]
                else:
                    Q = qoi(to_tensors[0]
                           ) if len(to_tensors) == 1 else qoi(to_tensors)

                    grads = [
                        get_backend().gradient(q, z)[0] for q in Q
                    ] if isinstance(Q, DATA_CONTAINER_TYPE
                                   ) else get_backend().gradient(Q, z)[0]
                    grads = grads[0] if isinstance(
                        grads, DATA_CONTAINER_TYPE
                    ) and len(grads) == 1 else grads
                    grads = [
                        attribution_cut.access_layer(g) for g in grads
                    ] if isinstance(grads, DATA_CONTAINER_TYPE
                                   ) else attribution_cut.access_layer(grads)
                    self._cached_gradient_tensors[gradient_tensor_key] = grads
                z_grads.append(grads)

        grad_flat = ModelWrapper._flatten(z_grads)

        gradients = [self._run_session(g, feed_dict) for g in grad_flat]

        gradients = ModelWrapper._unflatten(gradients, z_grads)
        return gradients[0] if len(gradients) == 1 else gradients
