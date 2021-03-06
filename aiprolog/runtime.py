#!/usr/bin/env python
# -*- coding: utf-8 -*- 

#
# Copyright 2015, 2016, 2017 Guenter Bartsch
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
# HAL-Prolog runtime with builtin predicates for AI use
#

import sys
import datetime
import dateutil.parser
import time
import pytz # $ pip install pytz
import rdflib
from rdflib.plugins.sparql.parserutils import CompValue
import logging

from tzlocal import get_localzone # $ pip install tzlocal

from zamiaprolog.runtime import PrologRuntime
from zamiaprolog.errors  import PrologRuntimeError
from zamiaprolog.logic   import NumberLiteral, StringLiteral, ListLiteral, Variable, Predicate
from pl2rdf              import pl_to_rdf, pl_literal_to_rdf, prolog_to_filter_expression, rdf_to_pl
from nltools.tokenizer   import tokenize
from nltools.misc        import edit_distance

import model

CONTEXT_GRAPH_NAME = u'http://ai.zamia.org/context'
KB_PREFIX          = u'http://ai.zamia.org/kb/'
USER_PREFIX        = u'http://ai.zamia.org/kb/user/'
USER_PROP_PREFIX   = u'http://ai.zamia.org/kb/user/prop/'
CURIN              = u'http://ai.zamia.org/kb/curin'
DEFAULT_USER       = USER_PREFIX + u'default'
TEST_USER          = USER_PREFIX + u'test'
TEST_TIME          = time.mktime(datetime.datetime(2016,12,06,13,28,6).timetuple())
MAX_CONTEXT_LEN    = 6

ACTION_VARNAME     = '__ACTION__'

def builtin_context_get(g, pe):

    """ context_get(+Name, -Value) """

    pe._trace ('CALLED BUILTIN context_get', g)

    pred = g.terms[g.inx]
    args = pred.args
    if len(args) != 2:
        raise PrologRuntimeError('context_get: 2 args expected.')

    key     = args[0].name
    arg_v   = pe.prolog_get_variable(args[1], g.env)

    v = pe.read_context(key)
    if not v:
        # import pdb; pdb.set_trace()
        return False

    g.env[arg_v] = v

    return True

def builtin_context_get_fn(pred, env, rt):

    """ context_get(+Name) """

    rt._trace_fn ('CALLED FUNCTION context_get', g)

    args = pred.args
    if len(args) != 1:
        raise PrologRuntimeError('context_get: 1 arg expected.')

    key     = args[0].name

    v = pe.read_context(key)
    if not v:
        return ListLiteral([])

    return v

def builtin_action_context_set(pe, args):

    """ context_set(+Name, +Value) """

    logging.debug ('CALLED BUILTIN ACTION context_set %s' % repr(args))

    if len(args) != 2:
        raise PrologRuntimeError('context_push: 2 args expected.')

    key   = args[0].name
    # value = pe.prolog_eval(args[1], g.env)
    value = args[1]

    # print u"builtin_set_context: %s -> %s" % (key, unicode(value))
    pe.write_context(key, value)

def builtin_action_context_push(pe, args):

    """ context_push(+Name, +Value) """

    logging.debug ('CALLED BUILTIN ACTION context_push %s' % repr(args))

    if len(args) != 2:
        raise PrologRuntimeError('context_push: 2 args expected.')

    key   = args[0].name
    value = args[1]

    # print u"builtin_set_context: %s -> %s" % (key, unicode(value))
    pe.push_context(key, value)


def builtin_context_score(g, pe):

    """ context_score(+Name, ?Value, +Points, ?Score [, +MinPoints]) """

    pe._trace ('CALLED BUILTIN context_score', g)

    pred = g.terms[g.inx]
    args = pred.args
    if len(args) < 4:
        raise PrologRuntimeError('context_score: at least 4 args expected.')
    if len(args) > 5:
        raise PrologRuntimeError('context_score: max 5 args expected.')

    key     = args[0].name
    value   = pe.prolog_eval(args[1], g.env)
    points  = pe.prolog_get_float(args[2], g.env)
    scorev  = pe.prolog_get_variable(args[3], g.env)

    if len(args) == 5:
        min_score = pe.prolog_get_float(args[4], g.env)
    else:
        min_score = 0.0

    score = g.env[scorev].f if scorev in g.env else 0.0

    if value:

        stack = pe.read_context(key)

        if stack:
            i = 1
            for v in stack.l:
                if v == value:
                    score += points / float(i)
                    break
                i += 1

        if score < min_score:
            return False
        g.env[scorev] = NumberLiteral(score)
        return True

    if not isinstance (args[1], Variable):
        raise PrologRuntimeError(u'score_context: arg 2 literal or variable expected, %s found instead.' % unicode(args[1]))

    res = []

    stack = pe.read_context(key)
    if stack:
        i = 1
        for v in stack.l:
            s = score + points / float(i)
            if s >= min_score:
                res.append({ 
                             args[1].name : v, 
                             scorev       : NumberLiteral(score + points / float(i))
                            })
            i += 1
    else:
        if score >= min_score:
            res.append({ 
                         args[1].name : ListLiteral([]), 
                         scorev       : NumberLiteral(score)
                        })

    return res


def _queue_action(g, action):

    if not ACTION_VARNAME in g.env:
        g.env[ACTION_VARNAME] = []

    g.env[ACTION_VARNAME].append( action )

def builtin_action(g, pe):

    pe._trace ('CALLED BUILTIN action', g)

    pred = g.terms[g.inx]
    args = pred.args

    evaluated_args = map (lambda v: pe.prolog_eval(v, g.env), args)

    _queue_action (g, evaluated_args)

    return True

def builtin_say(g, pe):

    """ say ( +Lang, +Str ) """

    pe._trace ('CALLED BUILTIN say', g)

    pred = g.terms[g.inx]
    args = pred.args
    if len(args) != 2:
        raise PrologRuntimeError('say: 2 args expected.')

    arg_L   = pe.prolog_eval(args[0], g.env).name
    arg_S   = pe.prolog_get_string(args[1], g.env)

    _queue_action (g, [Predicate('say'), arg_L, arg_S] )

    return True

def _eoa (g, pe, score):

    if not (ACTION_VARNAME in g.env):
        raise PrologRuntimeError('eoa: no action defined.')

    pe.end_action(g.env[ACTION_VARNAME], score)

    del g.env[ACTION_VARNAME]

def builtin_eoa(g, pe):

    """ eoa ( [+Score] ) """

    pe._trace ('CALLED BUILTIN eoa', g)

    pred = g.terms[g.inx]
    args = pred.args

    if len(args)>1:
        raise PrologRuntimeError('eoa: max 1 arg expected.')

    score = 0.0
    if len(args)>0:
        score = pe.prolog_get_float(args[0], g.env)

    _eoa (g, pe, score)

    return True

def builtin_say_eoa(g, pe):

    """ say_eoa ( +Lang, +Str, [+Score] ) """

    pe._trace ('CALLED BUILTIN say_eoa', g)

    pred = g.terms[g.inx]
    args = pred.args

    if len(args) < 2:
        raise PrologRuntimeError('say_eoa: at least 2 args expected.')
    if len(args) > 3:
        raise PrologRuntimeError('say_eoa: max 3 args expected.')

    arg_L   = pe.prolog_eval(args[0], g.env).name
    arg_S   = pe.prolog_get_string(args[1], g.env)

    _queue_action (g, [Predicate('say'), arg_L, arg_S] )

    score = 0.0
    if len(args)>2:
        score = pe.prolog_get_float(args[2], g.env)

    _eoa (g, pe, score)

    return True

def builtin_rdf(g, pe):

    pe._trace ('CALLED BUILTIN rdf', g)

    return _rdf_exec (g, pe)

def builtin_rdf_lists(g, pe):

    pe._trace ('CALLED BUILTIN rdf_lists', g)

    return _rdf_exec (g, pe, generate_lists=True)

def _rdf_exec (g, pe, generate_lists=False):

    # rdflib.plugins.sparql.parserutils.CompValue
    #
    # class CompValue(OrderedDict):
    #     def __init__(self, name, **values):
    #
    # SelectQuery(
    #   p =
    #     Project(
    #       p =
    #         LeftJoin(
    #           p2 =
    #             BGP(
    #               triples = [(rdflib.term.Variable(u'leaderobj'), rdflib.term.URIRef(u'http://dbpedia.org/ontology/leader'), rdflib.term.Variable(u'leader'))]
    #               _vars = set([rdflib.term.Variable(u'leaderobj'), rdflib.term.Variable(u'leader')])
    #             )
    #           expr =
    #             TrueFilter(
    #               _vars = set([])
    #             )
    #           p1 =
    #             BGP(
    #               triples = [(rdflib.term.Variable(u'leader'), rdflib.term.URIRef(u'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'), rdflib.term.URIRef(u'http://schema.org/Person')), (rdflib.term.Variable(u'leader'), rdflib.term.URIRef(u'http://www.w3.org/2000/01/rdf-schema#label'), rdflib.term.Variable(u'label'))]
    #               _vars = set([rdflib.term.Variable(u'label'), rdflib.term.Variable(u'leader')])
    #             )
    #           _vars = set([rdflib.term.Variable(u'leaderobj'), rdflib.term.Variable(u'label'), rdflib.term.Variable(u'leader')])
    #         )
    #       PV = [rdflib.term.Variable(u'leader'), rdflib.term.Variable(u'label'), rdflib.term.Variable(u'leaderobj')]
    #       _vars = set([rdflib.term.Variable(u'leaderobj'), rdflib.term.Variable(u'label'), rdflib.term.Variable(u'leader')])
    #     )
    #   datasetClause = None
    #   PV = [rdflib.term.Variable(u'leader'), rdflib.term.Variable(u'label'), rdflib.term.Variable(u'leaderobj')]
    #   _vars = set([rdflib.term.Variable(u'leaderobj'), rdflib.term.Variable(u'label'), rdflib.term.Variable(u'leader')])
    # )

    pred = g.terms[g.inx]
    args = pred.args
    # if len(args) == 0 or len(args) % 3 != 0:
    #     raise PrologRuntimeError('rdf: one or more argument triple(s) expected, got %d args' % len(args))

    distinct         = False
    triples          = []
    optional_triples = []
    filters          = []
    limit            = 0
    offset           = 0

    arg_idx          = 0
    var_map          = {} # string -> rdflib.term.Variable

    while arg_idx < len(args):

        arg_s = args[arg_idx]

        # check for optional structure
        if isinstance(arg_s, Predicate) and arg_s.name == 'optional':

            s_args = arg_s.args

            if len(s_args) != 3:
                raise PrologRuntimeError('rdf: optional: triple arg expected')

            arg_s = s_args[0]
            arg_p = s_args[1]
            arg_o = s_args[2]

            logging.debug ('rdf: optional arg triple: %s' %repr((arg_s, arg_p, arg_o)))

            optional_triples.append((pl_to_rdf(arg_s, g.env, pe, var_map, pe.kb), 
                                     pl_to_rdf(arg_p, g.env, pe, var_map, pe.kb), 
                                     pl_to_rdf(arg_o, g.env, pe, var_map, pe.kb)))

            arg_idx += 1

        # check for filter structure
        elif isinstance(arg_s, Predicate) and arg_s.name == 'filter':

            logging.debug ('rdf: filter structure detected: %s' % repr(arg_s.args))

            s_args = arg_s.args

            # transform multiple arguments into explicit and-tree

            pl_expr = s_args[0]
            for a in s_args[1:]:
                pl_expr = Predicate('and', [pl_expr, a])

            filters.append(prolog_to_filter_expression(pl_expr, g.env, pe, var_map, pe.kb))
            
            arg_idx += 1


        # check for distinct
        elif isinstance(arg_s, Predicate) and arg_s.name == 'distinct':

            s_args = arg_s.args
            if len(s_args) != 0:
                raise PrologRuntimeError('rdf: distinct: unexpected arguments.')

            distinct = True
            arg_idx += 1

        # check for limit/offset
        elif isinstance(arg_s, Predicate) and arg_s.name == 'limit':

            s_args = arg_s.args
            if len(s_args) != 1:
                raise PrologRuntimeError('rdf: limit: one argument expected.')

            limit = pe.prolog_get_int(s_args[0], g.env)
            arg_idx += 1

        elif isinstance(arg_s, Predicate) and arg_s.name == 'offset':

            s_args = arg_s.args
            if len(s_args) != 1:
                raise PrologRuntimeError('rdf: offset: one argument expected.')

            offset = pe.prolog_get_int(s_args[0], g.env)
            arg_idx += 1

        else:

            if arg_idx > len(args)-3:
                raise PrologRuntimeError('rdf: not enough arguments for triple')

            arg_p = args[arg_idx+1]
            arg_o = args[arg_idx+2]

            logging.debug ('rdf: arg triple: %s' %repr((arg_s, arg_p, arg_o)))

            triples.append((pl_to_rdf(arg_s, g.env, pe, var_map, pe.kb), 
                            pl_to_rdf(arg_p, g.env, pe, var_map, pe.kb), 
                            pl_to_rdf(arg_o, g.env, pe, var_map, pe.kb)))

            arg_idx += 3

    logging.debug ('rdf: triples: %s' % repr(triples))
    logging.debug ('rdf: optional_triples: %s' % repr(optional_triples))
    logging.debug ('rdf: filters: %s' % repr(filters))

    if len(triples) == 0:
        raise PrologRuntimeError('rdf: at least one non-optional triple expected')

    var_list = var_map.values()
    var_set  = set(var_list)

    p = CompValue('BGP', triples=triples, _vars=var_set)

    for t in optional_triples:
        p = CompValue('LeftJoin', p1=p, p2=CompValue('BGP', triples=[t], _vars=var_set),
                                  expr = CompValue('TrueFilter', _vars=set([])))

    for f in filters:
        p = CompValue('Filter', p=p, expr = f, _vars=var_set)

    if limit>0:
        p = CompValue('Slice', start=offset, length=limit, p=p, _vars=var_set)

    if distinct:
        p = CompValue('Distinct', p=p, _vars=var_set)

    algebra = CompValue ('SelectQuery', p = p, datasetClause = None, PV = var_list, _vars = var_set)
    
    result = pe.kb.query_algebra (algebra)

    logging.debug ('rdf: result (len: %d): %s' % (len(result), repr(result)))

    if len(result) == 0:
        return False

    if generate_lists:

        # bind each variable to list of values

        for binding in result:

            for v in binding.labels:

                l = binding[v]

                value = rdf_to_pl(l)

                if not v in g.env:
                    g.env[v] = ListLiteral([])

                g.env[v].l.append(value)

        return True

    else:

        # turn result into list of bindings

        res_bindings = []
        for binding in result:

            res_binding = {}

            for v in binding.labels:

                l = binding[v]

                value = rdf_to_pl(l)

                res_binding[v] = value

            res_bindings.append(res_binding)

        if len(res_bindings) == 0 and len(result)>0:
            res_bindings.append({}) # signal success

        logging.debug ('rdf: res_bindings: %s' % repr(res_bindings))

        return res_bindings

def builtin_action_rdf_assert(pe, args):

    """ rdf_assert (+S, +P, +O) """

    logging.debug ('CALLED BUILTIN ACTION rdf_assert %s' % repr(args))

    if len(args) != 3:
        raise PrologRuntimeError('rdf_assert: 3 args expected, got %d args' % len(args))

    arg_s = args[0]
    arg_p = args[1]
    arg_o = args[2]

    quads = [ (pl_to_rdf(arg_s, {}, pe, {}, pe.kb), 
               pl_to_rdf(arg_p, {}, pe, {}, pe.kb), 
               pl_to_rdf(arg_o, {}, pe, {}, pe.kb),
               pe.context_gn) ]

    pe.kb.addN(quads)


def builtin_uriref(g, pe):

    pe._trace ('CALLED BUILTIN uriref', g)

    pred = g.terms[g.inx]
    args = pred.args
    if len(args) != 2:
        raise PrologRuntimeError('uriref: 2 args expected.')

    if not isinstance(args[0], Predicate):
        raise PrologRuntimeError('uriref: first argument: predicate expected, %s found instead.' % repr(args[0]))

    if not isinstance(args[1], Variable):
        raise PrologRuntimeError('uriref: second argument: variable expected, %s found instead.' % repr(args[1]))

    g.env[args[1].name] = StringLiteral(pe.kb.resolve_aliases_prefixes(args[0].name))

    return True

def builtin_sparql_query(g, pe):

    pe._trace ('CALLED BUILTIN sparql_query', g)

    pred = g.terms[g.inx]
    args = pred.args
    if len(args) < 1:
        raise PrologRuntimeError('sparql_query: at least 1 argument expected.')

    query = pe.prolog_get_string(args[0], g.env)

    # logging.debug("builtin_sparql_query called, query: '%s'" % query)

    # run query

    result = pe.kb.query (query)

    # logging.debug("builtin_sparql_query result: '%s'" % repr(result))

    if len(result) == 0:
        return False

    # turn result into lists of literals we can then bind to prolog variables

    res_map  = {} 
    res_vars = {} # variable idx -> variable name

    for binding in result:

        for v in binding.labels:

            l = binding[v]

            value = rdf_to_pl(l)

            if not v in res_map:
                res_map[v] = []
                res_vars[binding.labels[v]] = v

            res_map[v].append(value)

    # logging.debug("builtin_sparql_query res_map : '%s'" % repr(res_map))
    # logging.debug("builtin_sparql_query res_vars: '%s'" % repr(res_vars))

    # apply bindings to environment vars

    v_idx = 0

    for arg in args[1:]:

        sparql_var = res_vars[v_idx]
        prolog_var = pe.prolog_get_variable(arg, g.env)
        value      = res_map[sparql_var]

        # logging.debug("builtin_sparql_query mapping %s -> %s: '%s'" % (sparql_var, prolog_var, value))

        g.env[prolog_var] = ListLiteral(value)

        v_idx += 1

    return True

def builtin_tokenize(g, pe):

    """ tokenize (+Lang, +Str, -Tokens) """

    pe._trace ('CALLED BUILTIN tokenize', g)

    pred = g.terms[g.inx]
    args = pred.args
    if len(args) != 3:
        raise PrologRuntimeError('tokenize: 3 args expected.')

    arg_lang    = pe.prolog_eval (args[0], g.env)
    if not isinstance(arg_lang, Predicate) or len(arg_lang.args) >0:
        raise PrologRuntimeError('tokenize: first argument: constant expected, %s found instead.' % repr(args[0]))

    arg_str     = pe.prolog_get_string   (args[1], g.env)
    arg_tokens  = pe.prolog_get_variable (args[2], g.env)

    g.env[arg_tokens] = ListLiteral(tokenize(arg_str, lang=arg_lang.name))

    return True

def builtin_edit_distance(g, pe):

    """" edit_distance (+Tokens1, +Tokens2, -Distance) """

    pe._trace ('CALLED BUILTIN edit_distance', g)

    pred = g.terms[g.inx]
    args = pred.args
    if len(args) != 3:
        raise PrologRuntimeError('edit_distance: 3 args expected.')

    arg_tok1  = pe.prolog_get_list     (args[0], g.env)
    arg_tok2  = pe.prolog_get_list     (args[1], g.env)
    arg_dist  = pe.prolog_get_variable (args[2], g.env)

    g.env[arg_dist] = NumberLiteral(edit_distance(arg_tok1.l, arg_tok2.l))

    return True

class AIPrologRuntime(PrologRuntime):

    def __init__(self, db, kb):

        super(AIPrologRuntime, self).__init__(db)

        # our knowledge base

        self.kb = kb

        # actions

        self.action_buffer   = []
        self.builtin_actions = {}

        self.register_builtin          ('action',          builtin_action)   # 
        self.register_builtin          ('eoa',             builtin_eoa)      # eoa: End Of Action ([+Score])

        self.register_builtin          ('say',             builtin_say)      # shortcut for action(say, lang, str) (+Lang, +Str)
        self.register_builtin          ('say_eoa',         builtin_say_eoa)  # shortcut for say followed by eoa (+Lang, +Str, [+Score])

        #
        # context related functions and predicates
        #

        self.context_gn = rdflib.Graph(identifier=CONTEXT_GRAPH_NAME)

        self.register_builtin          ('context_get',     builtin_context_get)         # context_get(+Name, -Value)
        self.register_builtin_function ('context_get',     builtin_context_get_fn)      # context_get(+Name)
        self.register_builtin_action   ('context_set',     builtin_action_context_set)  # context_set(+Name, +Value)
        self.register_builtin_action   ('context_push',    builtin_action_context_push) # context_push(+Name, +Value)
        self.register_builtin          ('context_score',   builtin_context_score)       # context_score(+Name, ?Value, +Points, -Score [, +MinPoints])

        # sparql / rdf

        self.register_builtin          ('sparql_query',    builtin_sparql_query)
        self.register_builtin          ('rdf',             builtin_rdf)
        self.register_builtin          ('rdf_lists',       builtin_rdf_lists)
        self.register_builtin_action   ('rdf_assert',      builtin_action_rdf_assert)   # rdf_assert (+S, +P, +O)
        self.register_builtin          ('uriref',          builtin_uriref)

        # natural language processing

        self.register_builtin          ('tokenize',        builtin_tokenize)            # tokenize (+Lang, +Str, -Tokens)
        self.register_builtin          ('edit_distance',   builtin_edit_distance)       # edit_distance (+Str1, +Str2, -Distance)


    def _builtin_action_wrapper (self, name, g, pe):


        l = [Predicate(name)]
        for arg in g.terms[g.inx].args:
            # logging.debug ('_builtin_action_wrapper: %s arg=%s' % (name, repr(arg)))
            value = pe.prolog_eval(arg, g.env)
            l.append(value)

        if not ACTION_VARNAME in g.env:
            g.env[ACTION_VARNAME] = []

        g.env[ACTION_VARNAME].append( l )
        return True

    def register_builtin_action (self, name, f):
        """ builtin actions are not executed right away but added to the current
            action buffer and will get executed when execute_builtin_actions() is called """

        self.builtin_actions[name] = f

        self.register_builtin (name, lambda g, pe: self._builtin_action_wrapper(name, g, pe))

    def execute_builtin_actions(self, abuf):

        # import pdb; pdb.set_trace()

        for action in abuf['actions']:

            if not isinstance(action[0], Predicate):
                continue
            name = action[0].name

            if not name in self.builtin_actions:
                continue
            self.builtin_actions[name](self, action[1:])

    def reset_actions(self):
        self.action_buffer = []

    def get_actions(self, highscore_only=True):

        if not highscore_only:
            return self.action_buffer

        # determine highest score

        highscore = 0
        for ab in self.action_buffer:

            logging.debug('get_actions: %s' % repr(ab))

            if ab['score'] > highscore:
                highscore = ab['score']

        # filter out any lower scoring action:

        hs_actions = []
        for ab in self.action_buffer:
            if ab['score'] < highscore:
                continue
            hs_actions.append(ab)
        return hs_actions

    def end_action(self, actions, score):

        self.action_buffer.append({'actions': actions, 'score': score})
        # logging.debug ('end_action -> %s' % repr(self.action_buffer))

    #
    # CURIN related helpers
    #

    def get_user (self):

        for q in self.kb.filter_quads(s=CURIN, p=KB_PREFIX + u'user'):
            return q[2]

        return None

    def read_curin (self, key):

        user = self.get_user()

        for q in self.kb.filter_quads(s=user, p=USER_PROP_PREFIX + key):
            return rdf_to_pl(q[2])

        user = DEFAULT_USER

        for q in self.kb.filter_quads(s=user, p=USER_PROP_PREFIX + key):
            return rdf_to_pl(q[2])

        return None

    #
    # manage stored contexts in db
    #

    def read_context (self, key):

        user = self.get_user()

        for q in self.kb.filter_quads(s=user, p=USER_PROP_PREFIX + key):
            return rdf_to_pl(q[2])

        user = DEFAULT_USER

        for q in self.kb.filter_quads(s=user, p=USER_PROP_PREFIX + key):
            return rdf_to_pl(q[2])

        return None

    def write_context (self, key, value):

        # import pdb; pdb.set_trace()

        user = self.get_user()

        v  = pl_literal_to_rdf(value, self.kb)

        self.kb.remove (  (user, USER_PROP_PREFIX + key, None, self.context_gn)  )
        self.kb.addN   ([ (user, USER_PROP_PREFIX + key,    v, self.context_gn) ])


    def push_context (self, key, value):

        l = self.read_context(key)

        # logging.debug ('context %s before push: %s' % (key, l))

        if not l:
            l = ListLiteral([])
        l.l.insert(0, value)

        l.l = l.l[:MAX_CONTEXT_LEN]

        # logging.debug ('context %s after push: %s' % (key, l))

        self.write_context (key, l)

