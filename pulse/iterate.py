#!/usr/bin/env python
import numpy as np
import operator as op
import dolfin

try:
    from dolfin_adjoint import Function, Constant
    has_dolfin_adjoint = True
except ImportError:
    from dolfin import Function, Constant
    has_dolfin_adjoint = False


from . import numpy_mpi
from . import parameters, annotation
from .mechanicsproblem import SolverDidNotConverge
from .dolfin_utils import get_constant
from .utils import make_logger

logger = make_logger(__name__, parameters['log_level'])


class Enlisted(tuple):
    pass


def enlist(x):
    if isinstance(x, (list, tuple, np.ndarray)):
        return x
    else:
        return Enlisted([x])


def delist(x):
    if isinstance(x, Enlisted):
        assert len(x) == 1
        return x[0]
    else:
        return x


def copy(f, deepcopy=True, name='copied_function'):
    """
    Copy a function. This is to ease the integration
    with dolfin adjoint where copied fuctions are annotated.
    """

    if isinstance(f, (dolfin.Function, Function)):
        if has_dolfin_adjoint:
            try:
                return f.copy(deepcopy=deepcopy, name=name)
            except TypeError:
                return f.copy(deepcopy=deepcopy)
        else:
            return f.copy(deepcopy=deepcopy)
    elif isinstance(f, dolfin.Constant):
        return dolfin.Constant(f, name=name)
    elif isinstance(f, Constant):
        return Constant(f, name=name)
    elif isinstance(f, (float, int)):
        return f
    elif isinstance(f, Enlisted):
        l = []
        for fi in f:
            l.append(copy(fi))
        return enlist(l)
        
    elif isinstance(f, (list, tuple)):
        l = []
        for fi in f:
            l.append(copy(fi))
        return tuple(l)
    else:
        return f


def constant2float(const):
    """
    Convert a :class:`dolfin.Constant` to float
    """
    const = get_constant(const)
    try:
        c = float(const)
    except TypeError:
        try:
            c = np.zeros(len(const))
            const.eval(c, c)
        except Exception as ex:
            logger.warning(ex, exc_info=True)
            return const

    return c


def get_delta(new_control, c0, c1):
    """
    Get extrapolation parameter used in the continuation step.
    """
    if isinstance(c0, (Constant, dolfin.Constant)):
        c0 = constant2float(c0)
        c1 = constant2float(c1)
        new_control = constant2float(new_control)

    if isinstance(new_control, (int, float)):
        return (new_control - c0) / float(c1 - c0)

    elif isinstance(new_control, (tuple, np.ndarray, list)):
        c0 = [constant2float(c) for c in c0]
        c1 = [constant2float(c) for c in c1]
        new_control = [constant2float(c) for c in new_control]
        return (new_control[0] - c0[0]) / float(c1[0] - c0[0])

    elif isinstance(new_control, (dolfin.GenericVector, dolfin.Vector)):
        new_control_arr = numpy_mpi.gather_broadcast(new_control.get_local())
        c0_arr = numpy_mpi.gather_broadcast(c0.get_local())
        c1_arr = numpy_mpi.gather_broadcast(c1.get_local())
        return (new_control_arr[0] - c0_arr[0]) / float(c1_arr[0] - c0_arr[0])

    elif isinstance(new_control, (dolfin.Function, Function)):
        new_control_arr = numpy_mpi.\
                          gather_broadcast(new_control.vector().get_local())
        c0_arr = numpy_mpi.gather_broadcast(c0.vector().get_local())
        c1_arr = numpy_mpi.gather_broadcast(c1.vector().get_local())
        return (new_control_arr[0] - c0_arr[0]) / float(c1_arr[0] - c0_arr[0])


def get_diff(current, target):
    """
    Get difference between current and target value
    """

    if isinstance(target, (Function, dolfin.Function)):
        diff = target.vector() - current.vector()

    elif isinstance(target, (Constant, dolfin.Constant)):
        diff = np.subtract(constant2float(target),
                           constant2float(current))
    elif isinstance(target, (tuple, list)):
        diff = np.subtract([constant2float(t) for t in target],
                           [constant2float(c) for c in current])
    else:
        try:
            diff = np.subtract(target, current)
        except Exception as ex:
            logger.error(ex)
            raise ValueError(("Unable to compute diff with type {}"
                              "").format(type(current)))

    return squeeze(diff)


def squeeze(x):

    try:
        y = np.squeeze(x)
    except:
        return x
    else:
        try:
            shape = np.shape(y)
        except:
            return y
        else:
            if len(shape) == 0:
                return float(y)
            else:
                return y


def get_initial_step(current, target, nsteps=5):
    """
    Estimate the step size needed to step from current to target
    in `nsteps`.
    """

    diff = get_diff(current, target)

    if isinstance(diff, dolfin.GenericVector):
        max_diff = dolfin.norm(diff, 'linf')
        step = Function(current.function_space())
        step.vector().axpy(1.0 / float(nsteps), diff)

    else:
        max_diff = abs(np.max(diff))
        step = diff / float(nsteps)
        
    logger.debug(("Intial number of steps: {} with step size {}"
                  "").format(nsteps, step))

    return step


def step_too_large(current, target, step):
    """
    Check if `current + step` exceeds `target`
    """

    if isinstance(target, (dolfin.Function, Function)):
        target = numpy_mpi.gather_broadcast(target.vector().get_local())
    elif isinstance(target, (Constant, dolfin.Constant)):
        target = constant2float(target)

    if isinstance(current, (dolfin.Function, Function)):
        current = numpy_mpi.gather_broadcast(current.vector().get_local())
    elif isinstance(current, (Constant, dolfin.Constant)):
        current = constant2float(current)

    if isinstance(step, (dolfin.Function, Function)):
        step = numpy_mpi.gather_broadcast(step.vector().get_local())
    elif isinstance(step, (Constant, dolfin.Constant)):
        step = constant2float(step)
        

    if isinstance(target, (float, int)):
        comp = op.gt if current < target else op.lt
        return comp(current + step, target)
    else:
        assert hasattr(target, "__len__")

        too_large = []
        for (c, t, s) in zip(current, target, step):

            try:
                too_large.append(step_too_large(c, t, s))
            except:
                from IPython import embed; embed()
                exit()
            # t = constant2float(t)
            # c = constant2float(c)
            # s = constant2float(s)

            # comp = op.gt if c < t else op.lt
            # too_large.append(comp(c+s, t))

    return np.any(too_large)


def iterate(problem, control, target,
            continuation=True, max_adapt_iter=8,
            adapt_step=True, old_states=None, old_controls=None,
            max_nr_crash=20, max_iters=40,
            initial_number_of_steps=5):

    """
    Using the given problem, iterate control to given target.

    Arguments
    ---------
    problem : pulse.MechanicsProblem
        The problem
    control : dolfin.Function or dolfin.Constant
        The control
    target: dolfin.Function, dolfin.Constant, tuple or float
        The target value. Typically a float if target is LVP, a tuple
        if target is (LVP, RVP) and a function if target is gamma.
    continuation: bool
        Apply continuation for better guess for newton problem
        Note: Replay test seems to fail when continuation is True,
        but taylor test passes
    max_adapt_iter: int
        If number of iterations is less than this number and adapt_step=True,
        then adapt control step. Default: 8
    adapt_step: bool
        Adapt / increase step size when sucessful iterations are achevied.
    old_states: list
        List of old controls to help speed in the continuation
    """

    iterator = Iterator(problem=problem,
                        control=control,
                        target=target,
                        continuation=continuation,
                        max_adapt_iter=max_adapt_iter,
                        adapt_step=adapt_step,
                        old_states=old_states,
                        old_controls=old_controls,
                        max_nr_crash=max_nr_crash,
                        max_iters=max_iters,
                        initial_number_of_steps=initial_number_of_steps)
    
    return iterator.solve()



class Iterator(object):
    """
    Iterator
    """
    _control_types = (Function, dolfin.Function,
                      Constant, dolfin.Constant)
    
    def __init__(self,
                 problem,
                 control,
                 target,
                 old_states=None,
                 old_controls=None,
                 **params
    ):
        logger.setLevel(parameters['log_level'])

        self.parameters = Iterator.default_parameters()
        self.parameters.update(params)

        self.old_controls = () if old_controls is None else old_controls
        self.old_states = () if old_states is None else old_states
        self.problem = problem
        self._check_control(control)
        self._check_target(target)

        self.control_values = [copy(delist(self.control), deepcopy=True,
                                    name='previous control')]
        self.prev_states = [copy(self.problem.state, deepcopy=True,
                                 name='previous state')]

        self.step = get_initial_step(self.control, self.target,
                                     self.parameters['initial_number_of_steps'])


    @staticmethod
    def default_parameters():
        return dict(continuation=True,
                    max_adapt_iter=8,
                    adapt_step=True,
                    max_nr_crash=20,
                    max_iters=40,
                    initial_number_of_steps=5)
        
    @property
    def step(self):
        return self._step

    @step.setter
    def step(self, step):
        self._step = enlist(squeeze(step))

    def solve(self):

        self.ncrashes = 0
        self.niters = 0
        while not self.target_reached():

            self.niters += 1
            if self.ncrashes > self.parameters['max_nr_crash'] \
               or self.niters > self.parameters['max_iters']:

                self.problem.reinit(self.prev_states[0])
                self.assign_new_control(self.control_values[0])
                
                raise SolverDidNotConverge

            prev_state = self.prev_states[-1]
            prev_control = enlist(self.control_values[-1])

            # Check if we are close
            if step_too_large(prev_control, self.target, self.step):
                self.change_step_for_final_iteration(prev_control)

            self.increment_control()

            if self.parameters['continuation']:
                self.continuation_step()


            logger.info("Try new control")
            self.print_control()
            try:
                nliter, nlconv = self.problem.solve()

            except SolverDidNotConverge as ex:
                logger.debug(ex)
                logger.info("\nNOT CONVERGING")
                logger.info("Reduce control step")
                self.ncrashes += 1
                self.assign_control(prev_control)

                # Assign old state
                logger.debug("Assign old state")
                self.problem.state.vector().zero()
                self.problem.reinit(prev_state)

                self.change_step_size(0.5)

        else:
            ncrashes = 0
            logger.info("\nSUCCESFULL STEP:")

            if not self.target_reached():

                if nliter < self.parameters['max_adapt_iter'] and\
                   self.parameters['adapt_step']:
                    self.change_step_size(1.5)
                    msg = "Adapt step size. New step size: {:.2f}".format(self.step[0])
                    logger.info(msg)

                self.control_values.append(copy(delist(self.control), deepcopy=True,
                                           name='Previous control'))

                self.prev_states.append(copy(self.problem.state, deepcopy=True,
                                             name='Previous state'))
        return self.prev_states, self.control_values

    def change_step_size(self, factor):
        self.step = factor * delist(self.step)

    def print_control(self):
        msg = 'Current control: '
        

        def print_control(cs, msg):
            controls = [constant2float(c) for c in cs]
            
            if len(controls) > 3:
                msg += ('\n\tMin:{:.2f}\tMean:{:.2f}\tMax:{:.2f}'
                        '').format(np.min(controls),
                                   np.mean(controls),
                                   np.max(controls))
            else:
                cs = []
                for c in controls:
                    if hasattr(c, '__len__'):
                        print_control(c, msg)
                    else:
                        cs.append(c)
                if cs:
                    msg += ','.join(['{:.2f}'.format(c) for c in cs])
            return msg

        msg = print_control(self.control, msg)
        logger.info(msg)


    def continuation_step(self):
        
        first_step = len(self.prev_states) < 2
        if first_step:
            return

        c0, c1 = self.control_values[-2:]
        s0, s1 = se.f.prev_states[-2:]

        delta = get_delta(self.control, c0, c1)

        if has_dolfin_adjoint and annotation.annotate:
            w = dolfin.Function(self.problem.state.function_space())

            w.vector().zero()
            w.vector().axpy(1.0 - delta, s0.vector())
            w.vector().axpy(delta, s1.vector())
            self.problem.reinit(w, annotate=True)
        else:
            self.problem.state.vector().zero()
            self.problem.state.vector().axpy(1.0 - delta, s0.vector())
            self.problem.state.vector().axpy(delta, s1.vector())

    def increment_control(self):
        for c, s in zip(self.control, self.step):
            if isinstance(c, (dolfin.Function, Function)):
                c_arr = numpy_mpi.gather_broadcast(c.vector().get_local())
                c_tmp = Function(c.function_space())
                c_tmp.vector()[:] = c_arr + s
                c.assign(c_tmp)
            else:
                c_arr = c
                c.assign(Constant(constant2float(c) + s))

    def assign_control(self, new_control):
        for c, n in zip(self.control, enlist(new_control)):
            c.assign(constant2float(n))
        


    def change_step_for_final_iteration(self, prev_control):
        """Change step size so that target is 
        reached in the next iteration
        """
        logger.info("Change step size for final iteration")
        
        
        if isinstance(self.step, (dolfin.Function, Function)):
            step = Function(self.target.function_space())
            step.vector().axpy(1.0, self.target.vector())
            step.vector().axpy(-1.0, prev_control.vector())
        elif isinstance(step, (list, np.ndarray, tuple)):
            step = np.array([constant2float(t) - constant2float(c)
                             for (t, c) in zip(self.target, enlist(prev_control))])
        else:
            step = self.target - prev_control
                    
        self.step = step
        
    def _check_target(self, target):

        target = enlist(target)

        targets = []
        for tar in target:

            try:
                t = get_constant(tar)
            except TypeError:
                msg = ('Unable to convert target for type {} '
                       'to a constant').format(type(target))
                raise TypeError(msg)
            targets.append(t)
        
        self.target = tuple(targets)


    def _check_control(self, control):

        control = enlist(control)
        
        # Control has to be either a function or
        # a constant
        for c in control:
            msg = ('Expected control parameters to be of type {}, '
                   'got {}').format(self._control_types, type(c))
            assert isinstance(c, self._control_types), msg

        self.control = control

    def assign_new_control(self, new_control):

        for c, n in zip(self.control, new_control):
            try:
                c.assign(n)
            except TypeError:
                c.assign(Constant(n))

    @property
    def ncontrols(self):
        """Number of controls
        """
        return len(self.control)

    
    def target_reached(self):
        """Check if control and target are the same
        """
        diff = get_diff(self.control, self.target)

        if isinstance(diff, dolfin.GenericVector):
            diff.abs()
            max_diff = diff.max()
            
        else:
            
            max_diff = np.max(abs(diff))
            
        reached = max_diff < 1e-6
        if reached:
            logger.info("Check target reached: YES!")
        else:
            logger.info("Check target reached: NO")
            logger.info("Maximum difference: {:.3e}".format(max_diff))
            
        return reached
        
        
    
