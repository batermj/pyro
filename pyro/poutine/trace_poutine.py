from __future__ import absolute_import, division, print_function

from .poutine import Messenger, Poutine
from .trace import Trace
from .util import site_is_subsample


def identify_dense_edges(trace):
    """
    Modifies a trace in-place by adding all edges based on the
    `cond_indep_stack` information stored at each site.
    """
    for name, node in trace.nodes.items():
        if site_is_subsample(node):
            continue
        if node["type"] == "sample":
            for past_name, past_node in trace.nodes.items():
                if site_is_subsample(node):
                    continue
                if past_node["type"] == "sample":
                    if past_name == name:
                        break
                    past_node_independent = False
                    for query, target in zip(node["cond_indep_stack"], past_node["cond_indep_stack"]):
                        if query.name == target.name and query.counter != target.counter:
                            past_node_independent = True
                            break
                    if not past_node_independent:
                        trace.add_edge(past_name, name)


class TraceMessenger(Messenger):
    """
    Execution trace messenger.

    A TraceMessenger records the input and output to every pyro primitive
    and stores them as a site in a Trace().
    This should, in theory, be sufficient information for every inference algorithm
    (along with the implicit computational graph in the Variables?)

    We can also use this for visualization.
    """

    def __init__(self, graph_type=None):
        """
        :param string graph_type: string that specifies the type of graph
            to construct (currently only "flat" or "dense" supported)
        """
        super(TraceMessenger, self).__init__()
        if graph_type is None:
            graph_type = "flat"
        assert graph_type in ("flat", "dense")
        self.graph_type = graph_type

    def __exit__(self, *args, **kwargs):
        """
        Adds appropriate edges based on cond_indep_stack information
        upon exiting the context.
        """
        if self.graph_type == "dense":
            identify_dense_edges(self.trace)
        return super(TraceMessenger, self).__exit__(*args, **kwargs)

    def get_trace(self):
        """
        :returns: data structure
        :rtype: pyro.poutine.Trace

        Helper method for a very common use case.
        Returns a shallow copy of ``self.trace``.
        """
        return self.trace.copy()

    def _reset(self):
        tr = Trace(graph_type=self.graph_type)
        tr.add_node("_INPUT",
                    name="_INPUT", type="input",
                    args=self.trace.nodes["_INPUT"]["args"],
                    kwargs=self.trace.nodes["_INPUT"]["kwargs"])
        self.trace = tr
        super(TraceMessenger, self)._reset()

    def _pyro_sample(self, msg):
        """
        :param msg: current message at a trace site.
        :returns: updated message

        Implements default pyro.sample Poutine behavior with an additional side effect:
        if the observation at the site is not None,
        then store the observation in self.trace
        and return the observation,
        else call the function,
        then store the return value in self.trace
        and return the return value.
        """
        name = msg["name"]
        if name in self.trace:
            site = self.trace.nodes[name]
            if site['type'] == 'param':
                # Cannot sample or observe after a param statement.
                raise RuntimeError("{} is already in the trace as a param".format(name))
            elif site['type'] == 'sample':
                # Cannot sample after a previous sample statement.
                raise RuntimeError("Multiple pyro.sample sites named '{}'".format(name))
        return None

    def _pyro_param(self, msg):
        """
        :param msg: current message at a trace site.
        :returns: updated message

        Implements default pyro.param Poutine behavior with an additional side effect:
        queries the parameter store with the site name and varargs
        and returns the result of the query.

        If the parameter doesn't exist, create it using the site varargs.
        If it does exist, grab it from the parameter store.
        Store the parameter in self.trace, and then return the parameter.
        """
        if msg["name"] in self.trace:
            if self.trace.nodes[msg['name']]['type'] == "sample":
                raise RuntimeError("{} is already in the trace as a sample".format(msg['name']))
        return None

    def _postprocess_message(self, msg):
        val = msg["value"]
        site = msg.copy()
        site.update(value=val)
        self.trace.add_node(msg["name"], **site)
        return None


class TracePoutine(Poutine):
    """
    Execution trace poutine.

    A TracePoutine records the input and output to every pyro primitive
    and stores them as a site in a Trace().
    This should, in theory, be sufficient information for every inference algorithm
    (along with the implicit computational graph in the Variables?)

    We can also use this for visualization.
    """

    def __init__(self, fn, graph_type=None):
        """
        :param fn: a stochastic function (callable containing pyro primitive calls)
        :param string graph_type: string that specifies the type of graph
            to construct (currently only "flat" or "dense" supported)
        """
        super(TracePoutine, self).__init__(TraceMessenger(graph_type), fn)

    def __call__(self, *args, **kwargs):
        """
        Runs the stochastic function stored in this poutine,
        with additional side effects.

        Resets self.trace to an empty trace,
        installs itself on the global execution stack,
        runs self.fn with the given arguments,
        uninstalls itself from the global execution stack,
        stores the arguments and return value of the function in special sites,
        and returns self.fn's return value
        """
        self.msngr.trace = Trace(graph_type=self.msngr.graph_type)
        self.trace = self.msngr.trace  # for compatibility with code that accesses trace directly
        self.msngr.trace.add_node("_INPUT",
                                  name="_INPUT", type="args",
                                  args=args, kwargs=kwargs)
        ret = super(TracePoutine, self).__call__(*args, **kwargs)
        self.msngr.trace.add_node("_RETURN", name="_RETURN", type="return", value=ret)
        return ret

    def get_trace(self, *args, **kwargs):
        """
        :returns: data structure
        :rtype: pyro.poutine.Trace

        Helper method for a very common use case.
        Calls this poutine and returns its trace instead of the function's return value.
        """
        self(*args, **kwargs)
        return self.msngr.get_trace()
