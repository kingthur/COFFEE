# This file is part of COFFEE
#
# COFFEE is Copyright (c) 2016, Imperial College London.
# Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

from sys import maxint
import operator

from base import *
from utils import *
from coffee.visitors import EstimateFlops
from expression import MetaExpr
from logger import log, COST_MODEL


class Temporary(object):

    """A Temporary stores useful information for a statement (e.g., an Assign
    or an AugmentedAssig) that computes a temporary variable; that is, a variable
    that is read in more than one place."""

    def __init__(self, node, main_loop, nest, reads=None, linear_reads_costs=None):
        self.level = -1
        self.pushed = False
        self.readby = []

        self.node = node
        self.main_loop = main_loop
        self.nest = nest
        self.reads = reads or []
        self.linear_reads_costs = linear_reads_costs or OrderedDict()
        self.flops = EstimateFlops().visit(node)

    @property
    def name(self):
        return self.symbol.symbol if self.symbol else None

    @property
    def rank(self):
        return self.symbol.rank if self.symbol else None

    @property
    def symbol(self):
        if isinstance(self.node, Writer):
            return self.node.lvalue
        elif isinstance(self.node, Symbol):
            return self.node
        else:
            return None

    @property
    def expr(self):
        if isinstance(self.node, Writer):
            return self.node.rvalue
        else:
            return None

    @property
    def urepr(self):
        return self.symbol.urepr

    @property
    def linear_reads(self):
        return self.linear_reads_costs.keys() if self.linear_reads_costs else []

    @property
    def loops(self):
        return zip(*self.nest)[0]

    @property
    def niters(self):
        return reduce(operator.mul, [l.size for l in self.loops], 1)

    @property
    def niters_after_licm(self):
        return reduce(operator.mul,
                      [l.size for l in self.loops if l is not self.main_loop], 1)

    @property
    def project(self):
        return len(self.linear_reads)

    @property
    def is_ssa(self):
        return self.symbol not in self.readby

    @property
    def is_static_init(self):
        return isinstance(self.expr, ArrayInit)

    def reconstruct(self):
        temporary = Temporary(self.node, self.main_loop, self.nest, list(self.reads),
                              OrderedDict(self.linear_reads_costs))
        temporary.level = self.level
        temporary.readby = list(self.readby)
        return temporary

    def __str__(self):
        return "%s: level=%d, flops/iter=%d, linear_reads=[%s], isread=[%s]" % \
            (self.symbol, self.level, self.flops,
             ", ".join([str(i) for i in self.linear_reads]),
             ", ".join([str(i) for i in self.readby]))


class CSEUnpicker(object):

    """Analyze loops in which some temporary variables are computed and, applying
    a cost model, decides whether to leave a temporary intact or inline it for
    creating factorization and code motion opportunities.

    The cost model exploits one particular property of loops, namely linearity in
    symbols (further information concerning loop linearity is available in the module
    ``expression.py``)."""

    def __init__(self, exprs, header, hoisted, decls, expr_graph):
        self.exprs = exprs
        self.header = header
        self.hoisted = hoisted
        self.decls = decls
        self.expr_graph = expr_graph

    @property
    def type(self):
        return self.exprs.values()[0].type

    @property
    def linear_dims(self):
        return self.exprs.values()[0].linear_dims

    def _push_temporaries(self, temporaries, trace, global_trace, ra):

        def is_pushable(temporary):
            # To be pushable ...
            if not temporary.is_ssa:
                # ... must be written only once
                return False
            if not temporary.readby:
                # ... must actually be read by some other temporaries (the output
                # variables are not)
                return False
            if temporary.is_static_init:
                # ... its rvalue must not be an array initializer
                return False
            pushed_in = [global_trace.get(rb.urepr) for rb in temporary.readby]
            pushed_in = set(rb.main_loop.children[0] for rb in pushed_in if rb)
            for s in temporary.reads:
                # ... all the read temporaries must be accessible in the loops in which
                # they will be pushed
                if s.urepr in global_trace and global_trace[s.urepr].pushed:
                    continue
                if any(l not in ra[self.decls[s.symbol]] for l in pushed_in):
                    return False
            return True

        to_replace, modified_temporaries = {}, OrderedDict()
        for t in temporaries:
            # Track temporaries to be pushed from /level-1/ into the later /level/s
            if not is_pushable(t):
                continue
            to_replace[t.symbol] = t.expr or t.symbol
            for rb in t.readby:
                modified_temporaries[rb.urepr] = trace.get(rb.urepr,
                                                           global_trace[rb.urepr])
            # The temporary is going to be pushed, so we can remove it as long as
            # it is not needed somewhere else
            if t.node in t.main_loop.body and all(rb.urepr in trace for rb in t.readby):
                global_trace[t.urepr].pushed = True
                t.main_loop.body.remove(t.node)
                self.decls.pop(t.name, None)

        # Transform the AST (note: node replacement must happend in the order
        # in which temporaries have been encountered)
        modified_temporaries = sorted(modified_temporaries.values(),
                                      key=lambda t: global_trace.keys().index(t.urepr))
        for t in modified_temporaries:
            ast_replace(t.node, to_replace, copy=True)
        replaced = [t.urepr for t in to_replace.keys()]

        # Update the temporaries
        for t in modified_temporaries:
            for r, c in t.linear_reads_costs.items():
                if r.urepr in replaced:
                    t.linear_reads_costs.pop(r)
                    r_linear_reads_costs = global_trace[r.urepr].linear_reads_costs
                    for p, p_c in r_linear_reads_costs.items() or [(r, 0)]:
                        t.linear_reads_costs[p] = c + p_c

    def _transform_temporaries(self, temporaries):
        from rewriter import ExpressionRewriter

        # Never attempt to transform the main expression
        temporaries = [t for t in temporaries if t.node not in self.exprs]

        lda = loops_analysis(self.header, key='symbol', value='dim')

        # Expand + Factorize
        rewriters = OrderedDict()
        for t in temporaries:
            expr_info = MetaExpr(self.type, t.main_loop.children[0], t.nest,
                                 tuple(l.dim for l in t.loops if l.is_linear))
            ew = ExpressionRewriter(t.node, expr_info, self.decls, self.header,
                                    self.hoisted, self.expr_graph)
            ew.replacediv()
            ew.expand(mode='all', lda=lda)
            ew.factorize(mode='adhoc', adhoc={i.urepr: [] for i in t.linear_reads}, lda=lda)
            ew.factorize(mode='heuristic')
            rewriters[t] = ew

        lda = loops_analysis(self.header, value='dim')

        # Code motion
        for t, ew in rewriters.items():
            ew.licm(mode='only_outlinear', lda=lda, global_cse=True)

    def _analyze_expr(self, expr, lda):
        finder = FindInstances(Symbol)
        reads = finder.visit(expr, ret=FindInstances.default_retval())[Symbol]
        reads = [s for s in reads if s.symbol in self.decls]
        syms = [s for s in reads if any(l in self.linear_dims for l in lda[s])]

        linear_reads_costs = OrderedDict()

        def wrapper(node, found=0):
            if isinstance(node, Symbol):
                if node in syms:
                    linear_reads_costs.setdefault(node, 0)
                    linear_reads_costs[node] += found
                return
            elif isinstance(node, (EmptyStatement, ArrayInit)):
                return
            elif isinstance(node, (Prod, Div)):
                found += 1
            operands = zip(*explore_operator(node))[0]
            for o in operands:
                wrapper(o, found)
        wrapper(expr)

        return reads, linear_reads_costs

    def _analyze_loop(self, loop, nest, lda, global_trace):
        trace = OrderedDict()

        for node in loop.body:
            if not isinstance(node, Writer):
                not_ssa = [trace[w] for w in in_written(node, key='urepr') if w in trace]
                for t in not_ssa:
                    t.readby.append(t.symbol)
                continue
            reads, linear_reads_costs = self._analyze_expr(node.rvalue, lda)
            for s in linear_reads_costs.keys():
                if s.urepr in global_trace:
                    temporary = global_trace[s.urepr]
                    temporary.readby.append(node.lvalue)
                    temporary = temporary.reconstruct()
                    temporary.level = -1
                    trace[s.urepr] = temporary
                else:
                    temporary = trace.setdefault(s.urepr, Temporary(s, loop, nest))
                    temporary.readby.append(node.lvalue)
            new_temporary = Temporary(node, loop, nest, reads, linear_reads_costs)
            new_temporary.level = max([trace[s.urepr].level for s
                                       in new_temporary.linear_reads] or [-2]) + 1
            trace[node.lvalue.urepr] = new_temporary

        return trace

    def _group_by_level(self, trace):
        levels = defaultdict(list)

        for temporary in trace.values():
            levels[temporary.level].append(temporary)
        return levels

    def _cost_cse(self, levels, bounds=None):
        if bounds is not None:
            lb, up = bounds[0], bounds[1] + 1
            levels = {i: levels[i] for i in range(lb, up)}
        cost = 0
        for level, temporaries in levels.items():
            cost += sum(t.flops*t.niters for t in temporaries)
        return cost

    def _cost_fact(self, trace, levels, bounds):
        # Check parameters
        bounds = bounds or (min(levels.keys()), max(levels.keys()))
        assert len(bounds) == 2 and bounds[1] >= bounds[0]
        assert bounds[0] in levels.keys() and bounds[1] in levels.keys()

        # Determine current costs of individual loop regions
        cse_cost = self._cost_cse(levels, (min(levels.keys()), bounds[0]))
        uptolevel_cost = cse_cost
        level_inloop_cost, total_outloop_cost, cse = 0, 0, 0

        # We are going to modify a copy of the temporaries dict
        new_trace = OrderedDict()
        for s, t in trace.items():
            new_trace[s] = t.reconstruct()

        best = (bounds[0], bounds[0], maxint)
        fact_levels = {k: v for k, v in levels.items() if k > bounds[0] and k <= bounds[1]}
        for level, temporaries in sorted(fact_levels.items(), key=lambda (i, j): i):
            level_inloop_cost = 0
            for t in temporaries:
                # The operation count, after fact+licm, outside /loop/, induced by /t/
                t_outloop_cost = 0
                # The operation count, after fact+licm, within /loop/, induced by /t/
                t_inloop_cost = 0

                # Calculate the operation count for /t/ if we applied expansion + fact
                linear_reads = []
                for read, cost in t.linear_reads_costs.items():
                    if read.urepr in new_trace:
                        linear_reads.extend(new_trace[read.urepr].linear_reads or
                                            [read.urepr])
                        t_outloop_cost += new_trace[read.urepr].project*cost
                    else:
                        linear_reads.extend([read.urepr])

                # Factorization will kill duplicates and increase the number of sums
                # in the outer loop
                fact_syms = {s.urepr if isinstance(s, Symbol) else s for s in linear_reads}
                t_outloop_cost += len(linear_reads) - len(fact_syms)

                # Note: if n=len(fact_syms), then we'll have n prods, n-1 sums
                t_inloop_cost += 2*len(fact_syms) - 1

                # Add to the total and scale up by the corresponding number of iterations
                total_outloop_cost += t_outloop_cost*t.niters_after_licm
                level_inloop_cost += t_inloop_cost*t.niters

                # Update the trace because we want to track the cost after "pushing" the
                # temporaries on which /t/ depends into /t/ itself
                new_trace[t.urepr].linear_reads_costs = {s: 1 for s in fact_syms}

            # Some temporaries at levels < /i/ may also appear in:
            # 1) subsequent loops
            # 2) levels beyond /i/
            for t in list(flatten([levels[j] for j in range(level)])):
                if any(rb.urepr not in new_trace for rb in t.readby) or \
                        any(new_trace[rb.urepr].level > level for rb in t.readby):
                    # Note: condition 1) is basically saying "if I'm read from
                    # a temporary that is not in this loop's trace, then I must
                    # be read in some other loops".
                    level_inloop_cost += t.flops*t.niters

            # Total cost = cost_after_fact_up_to_level + cost_inloop_cse
            #            = cost_hoisted_subexprs + cost_inloop_fact + cost_inloop_cse
            uptolevel_cost = cse_cost + total_outloop_cost + level_inloop_cost
            uptolevel_cost += self._cost_cse(fact_levels, (level + 1, bounds[1]))

            # Update the best alternative
            if uptolevel_cost < best[2]:
                best = (bounds[0], level, uptolevel_cost)

            cse = self._cost_cse(fact_levels, (level + 1, bounds[1]))

        log('CSE: unpicking between levels [%d, %d]:' % bounds, COST_MODEL)
        log('CSE: cost=%d (cse=%d, outloop=%d, inloop_fact=%d, inloop_cse=%d)' %
            (uptolevel_cost, cse_cost, total_outloop_cost, level_inloop_cost, cse), COST_MODEL)

        return best

    def unpick(self):
        # Collect all necessary info
        external_decls = [d for d in self.decls.values() if d.scope == EXTERNAL]
        fors = visit(self.header, info_items=['fors'])['fors']
        lda = loops_analysis(self.header, value='dim')
        ra = reachability_analysis(self.header, external_decls)

        # Collect all loops to be analyzed
        nests = OrderedDict()
        for nest in fors:
            for loop, parent in nest:
                if loop.is_linear:
                    nests[loop] = nest

        # Analyze loops
        global_trace = OrderedDict()
        mapper = OrderedDict()
        for loop, nest in nests.items():
            trace = self._analyze_loop(loop, nest, lda, global_trace)
            if trace:
                mapper[loop] = trace
                global_trace.update(trace)

        for loop, trace in mapper.items():
            # Compute the best cost alternative
            levels = self._group_by_level(trace)
            min_level, max_level = min(levels.keys()), max(levels.keys())
            current_cost = self._cost_cse(levels, (min_level, max_level))
            global_best = (min_level, min_level, current_cost)
            for i in sorted(levels.keys()):
                local_best = self._cost_fact(trace, levels, (i, max_level))
                if local_best[2] < global_best[2]:
                    global_best = local_best

            log("-- Best: [%d, %d] (cost=%d) --" % global_best, COST_MODEL)

            # Transform the loop
            for i in range(global_best[0] + 1, global_best[1] + 1):
                self._push_temporaries(levels[i-1], trace, global_trace, ra)
                self._transform_temporaries(levels[i])

        # Clean up
        for transformed_loop, nest in reversed(nests.items()):
            for loop, parent in nest:
                if loop == transformed_loop and not loop.body:
                    parent.children.remove(loop)