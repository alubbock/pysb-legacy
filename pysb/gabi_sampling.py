import pysb.bng
import numpy 
import re 
import ctypes
import scipy.interpolate
import sys
#from pysundials import cvode
from scipy.integrate import odeint
import QuantLib
from pysb.examples import simple_egfr
from pysb import integrate
import random

# Thee set of functions set up the system for annealing runs
# and provide the runner function as input to annealing

def spinner(i):
    spin = ("|", "/","-", "\\")
    print "\r[%s] %d"%(spin[i%4],i),
    sys.stdout.flush()

# reltol of 1.0e-3, relative error of ~1%. abstol of 1.0e-3, enough for values that oscillate in the hundreds to thousands
def odeinit(model, reltol=1.0e-3, abstol=1.0e-3, nsteps = 1000, itermaxstep = None):
    '''
    must be run to set up the environment for annealing with pysundials
    '''

    # Generate equations
    pysb.bng.generate_equations(model)
    # Get the size of the ODE array
    odesize = len(model.odes)

    # init the arrays we need
    yzero = numpy.zeros(odesize)  #initial values for yzero

    # assign the initial conditions
    for cplxptrn, ic_param in model.initial_conditions:
        speci = model.get_species_index(cplxptrn)
        yzero[speci] = ic_param.value

    # initialize y with the yzero values
    y = yzero.copy()

    # make a dict of ydot functions. notice the functions are in this namespace.
    # replace the kxxxx constants with elements from the params array
    rhs_exprs = []
    for i in range(0,odesize):
        # first get the function string from sympy, replace the the "sN" with y[N]
        tempstring = re.sub(r's(\d+)', lambda m: 'y[%s]'%(int(m.group(1))), str(model.odes[i]))
        # now replace the constants with 'p' array names; cycle through the whole list
        #for j in range(0, numparams):
        #    tempstring = re.sub('(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])'%(model.parameters[j].name),'p[%d]'%(j), tempstring)
        for j, parameter in enumerate(model.parameters):
            tempstring = re.sub('(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])' % parameter.name, 'p[%d]' % j, tempstring)
        # make a list of compiled rhs expressions which will be run by the integrator
        # use the ydots to build the function for analysis
        # (second arg is the "filename", useful for exception/debug output)
        rhs_exprs.append(compile(tempstring, '<ydot[%s]>' % i, 'eval'))

    # Create the structure to hold the parameters when calling the function
    # This results in a generic "p" array
    numparams = len(model.parameters)
    class UserData(ctypes.Structure):
        _fields_ = [('p', ctypes.c_float*numparams)]
    PUserData = ctypes.POINTER(UserData)
    data = UserData() 

    data.p[:] = [p.value for p in model.parameters]
    paramarray = numpy.array([p.value for p in model.parameters])

    def f(y, t):
        """ Evaluate dy/dt for y at time t """
        rhs_locals = {'y': y, 'p': data.p}
        ydot = numpy.zeros(len(model.odes))
        for i in range(0, len(model.odes)):
            ydot[i] = eval(rhs_exprs[i], rhs_locals)
        return ydot

    #list of outputs
    xout = numpy.zeros(nsteps)
    yout = numpy.zeros([nsteps, odesize])

    #initialize the arrays
    #print "Initial parameter values:", y
    xout[0] = 0.0 #CHANGE IF NEEDED
    #first step in yout
    for i in range(0, odesize):
        yout[0][i] = y[i]
    
    return [f, rhs_exprs, y, odesize, data, xout, yout, nsteps, yzero, reltol, abstol], paramarray


def odesolve(model, tfinal, envlist, params, useparams=None, tinit = 0.0, ic=True):
    '''
    the ODE equation solver tailored to work with the annealing algorithm
    model: the model object
    envlist: the list returned from annlinit
    params: the list of parameters that are being optimized with annealing 
    useparams: the parameter number to which params[i] corresponds
    tinit: initial time
    reltol: relative tolerance
    abstol: absolute tolerance
    ic: reinitialize initial conditions to a value in params or useparams
    '''
    (f, rhs_exprs, y, odesize, data, xout, yout, nsteps, yzero, reltol, abstol) = envlist

    #set the initial values and params in each run
    #all parameters are used in annealing. initial conditions are not, here
    if useparams is None:
        for i in range(len(params)):
            data.p[i] = params[i]
    else:
        #only a subset of parameters are used for annealing
        for i in range(len(useparams)):
            #print "changing parameter", model.parameters[useparams[i]],"data.p", data.p[useparams[i]],"to", params[i]
            data.p[useparams[i]] = params[i]

    # FIXME:
    # update yzero if initial conditions are being modified as part of the parameters
    # did it this way b/c yzero and data.p may not always be modified at the same time
    # the params list should NOT contain the initial conditions if they are not
    # to be used in the annealing... so this is a hack based on the fact that the
    # initial conditions are contained as part of the model.parameters list.
    #
    if ic is True:
        for cplxptrn, ic_param in model.initial_conditions:
            speci = model.get_species_index(cplxptrn)
            yzero[speci] = ic_param.value
            

    #reset initial concentrations
    y = yzero.copy()

    xout = numpy.linspace(tinit, tfinal, nsteps)

    yout = odeint(f, yzero, xout)

    #now deal with observables
    #obs_names = [name for name, rp in model.observable_patterns]
    yobs = numpy.zeros([len(model.observables), nsteps])
    
    #sum up the correct entities
    for i, obs in enumerate(model.observables):
        coeffs = obs.coefficients
        specs  = obs.species
        yobs[i] = (yout[:, specs] * coeffs).sum(1)

    #merge the x and y arrays for easy analysis
    xyobs = numpy.vstack((xout, yobs))

    return (xyobs,xout,yout, yobs)

def compare_data(xparray, simarray, xspairlist, vardata=False):
    """Compares two arrays of different size and returns the X^2 between them.
    Uses the X axis as the unit to re-grid both arrays. 
    xparray: experimental data
    xparrayaxis: which axis of xparray to use for simulation
    simarray: simulation data
    simarrayaxis: which axis of simarray to use for simulation
    """
    # this expects arrays of the form array([time, measurement1, measurement2, ...])
    # the time is assumed to be roughly the same for both and the 
    # shortest time will be taken as reference to regrid the data
    # the regridding is done using a b-spline interpolation
    # xparrayvar shuold be the variances at every time point
    #
    # FIXME FIXME FIXME FIXME
    # This prob should figure out the overlap of the two arrays and 
    # get a spline of the overlap. For now just assume the simarray domain
    # is bigger than the xparray. FIXME FIXME FIXME
    #rngmax = min(xparray[0].max(), simarray[0].max())
    #rngmin = round(rngmin, -1)
    #rngmax = round(rngmax, -1)
    #print "Time overlap range:", rngmin,"to", rngmax
    
    ipsimarray = numpy.zeros(xparray.shape[1])
    objout = []
   
    for i in range(len(xspairlist)):
        # create a b-spline of the sim data and fit it to desired range
        # import code
        # code.interact(local=locals())
        
        #some error checking
        #print "xspairlist length:", len(xspairlist[i])
        #print "xspairlist element type:", type(xspairlist[i])
        #print "xspairlist[i] elements:", xspairlist[i][0], xspairlist[i][1]
        assert type(xspairlist[i]) is tuple
        assert len(xspairlist[i]) == 2
        
        xparrayaxis = xspairlist[i][0]
        simarrayaxis = xspairlist[i][1]
        
        tck = scipy.interpolate.splrep(simarray[0], simarray[simarrayaxis])
        ipsimarray = scipy.interpolate.splev(xparray[0], tck) #xp x-coordinate values to extract from y splines
        
        # we now have x and y axis for the points in the model array
        # calculate the objective function
        #                        1
        # obj(t, params) = -------------(S_sim(t,params)-S_exp(t))^2
        #                  2*sigma_exp^2
        
        diffarray = ipsimarray - xparray[xparrayaxis]
        diffsqarray = diffarray * diffarray

        if vardata is True:
            #print "using XP VAR",xparrayaxis+1
            xparrayvar = xparray[xparrayaxis+1] # variance data provided in xparray in next column
        else:
            # assume a default variance
            xparrayvar = numpy.ones(xparray.shape[1])
            xparrayvar = xparray[xparrayaxis]*.1 # within 10%? FIXME: check w will about this
            xparrayvar = xparrayvar * xparrayvar

        xparrayvar = xparrayvar*2.0
        numpy.seterr(divide='ignore') # FIXME: added to remove the warnings... use caution!!
        objarray = diffsqarray / xparrayvar

        # check for inf in objarray, they creep up when there are near zero or zero values in xparrayvar
        for i in range(len(objarray)):
            if numpy.isinf(objarray[i]) or numpy.isnan(objarray[i]):
                #print "CORRECTING NAN OR INF. IN ARRAY"
                # print objarray
                objarray[i] = 1e-100 #zero enough

        #import code
        #code.interact(local=locals())

        objout.append(objarray.sum())
        #print "OBJOUT(%d,%d):%f  OBJOUT(CUM):%f"%(xparrayaxis, simarrayaxis, objarray.sum(), objout)
    #print "OBJOUT(total):", objout
    return numpy.asarray(objout)

def getgauss(sobolarr, params, standard_devs):
    """ map a set of sobol pseudo-random numbers to a range for parameter evaluation
    # sobol: sobol number array of the appropriate length
    # params: array of model parameters
    # standard_devs: standard deviations for each parameter, or a single
    # value to use for all parameters. 66% of values lie in range +- SD,
    # 95% in range SD/2
    """

    sobprmarr = numpy.zeros_like(sobolarr)
    #ub = numpy.zeros(len(params))
    #lb = numpy.zeros(len(params))

    from rpy2.robjects import FloatVector
    from rpy2.robjects.packages import importr

    # use R's qnorm inverse Gaussian transform
    qnorm = importr('stats').qnorm
    
    sobprmarr = [params*numpy.array(qnorm(FloatVector(sv),mean=1,sd=standard_devs)) for sv in sobolarr]

    # set upper/lower bounds for generic problem
    # for i in range(len(params)):
    #     if i in useparams:
    #         ub[i] = params[i] * pow(10,usemag)
    #         lb[i] = params[i] / pow(10,usemag)
    #     else:
    #         ub[i] = params[i] * pow(10, omag)
    #         lb[i] = params[i] / pow(10, omag)
    
    # see  for more info http://en.wikipedia.org/wiki/Exponential_family
#    sobprmarr = lb*(ub/lb)**sobolarr # map the [0..1] sobol array to values sampled over their omags

    # sobprmarr is the N x len(params) array for sobol analysis
    return sobprmarr

def getlog(sobolarr, params, omag=1, useparams=[], usemag=None):
    # map a set of sobol pseudo-random numbers to a range for parameter evaluation
    # sobol: sobol number array of the appropriate length
    # params: array of parameters
    # omag: order of magnitude over which params should be sampled. this is effectively 3 orders of magnitude when omag=1
    #

    sobprmarr = numpy.zeros_like(sobolarr)
    ub = numpy.zeros(len(params))
    lb = numpy.zeros(len(params))
    # set upper/lower bounds for generic problem
    for i in range(len(params)):
        if i in useparams:
            ub[i] = params[i] * pow(10,usemag)
            lb[i] = params[i] / pow(10,usemag)
        else:
            ub[i] = params[i] * pow(10, omag)
            lb[i] = params[i] / pow(10, omag)
    
    # see  for more info http://en.wikipedia.org/wiki/Exponential_family
    sobprmarr = lb*(ub/lb)**sobolarr # map the [0..1] sobol array to values sampled over their omags

    # sobprmarr is the N x len(params) array for sobol analysis
    return sobprmarr

def getlin(sobolarr, params, CV =.25, useparams=[], useCV=None):
    """ map a set of sobol pseudo-random numbers to a range for parameter evaluation

    sobol: sobol number array of the appropriate length
    params: array of parameters
    stdev: standard deviation for parameters, this assumes it is unknown for the sampling
    
    function maps the sobol (or any random) [0:1) array linearly to mean-2sigma < x < mean + 2sigma

    CV is the coefficient of variance, CV = sigma/mean
    """

    sobprmarr = numpy.zeros_like(sobolarr)

    ub = numpy.zeros(len(params))
    lb = numpy.zeros(len(params))
    # set upper/lower bounds for generic problem
    for i in range(len(params)):
        if i in useparams:
            ub[i] = params[i] + params[i]*useCV
            lb[i] = params[i] - params[i]*useCV
        else:
            ub[i] = params[i] + params[i]*CV
            lb[i] = params[i] - params[i]*CV
    
    # sobprmarr = (sobolarr*(ub-lb)) + lb #map the [0..1] sobol array to the values for integration
    if len(sobprmarr.shape) == 1:
        sobprmarr = (sobolarr*(ub-lb)) + lb
    elif len(sobprmarr.shape) == 2:
        for i in range(sobprmarr.shape[0]):
            sobprmarr[i] = (sobolarr[i]*(ub-lb)) + lb
    else:
        print "array shape not allowed... "
        

    # sobprmarr is the N x len(params) array for sobol analysis
    # lb is the lower bound of params
    # ub is the upper bound of params
    return sobprmarr


def genCmtx(sobmtxA, sobmtxB):
    """when passing the quasi-random sobol-treated A and B matrixes, this function iterates over all the possibilities
    and returns the C matrix for simulations.
    See e.g. Saltelli, Ratto, Andres, Campolongo, Cariboni, Gatelli, Saisana, Tarantola Global Sensitivity Analysis"""

    nparams = sobmtxA.shape[1] # shape 1 should be the number of params

    # allocate the space for the C matrix
    sobmtxC = numpy.array([sobmtxB]*nparams) 

    # Now we have nparams copies of sobmtxB. replace the i_th column of sobmtxC with the i_th column of sobmtxA
    for i in range(nparams):
        sobmtxC[i,:,i] = sobmtxA[:,i]

    return sobmtxC


def parmeval(model, sobmtxA, sobmtxB, sobmtxC, time, envlist, xpdata, xspairlist, ic=True, norm=True, vardata=False, useparams = None, fileobj=None):
    ''' Function parmeval calculates the yA, yB, and yC_i arrays needed for variance-based global sensitivity analysis
    as prescribed by Saltelli and derived from the work by Sobol.
    '''
    # 
    #

    # assign the arrays that will hold yA, yB and yC_n
    yA = numpy.zeros([sobmtxA.shape[0]] + [len(model.observable_patterns)])
    yB = numpy.zeros([sobmtxB.shape[0]] + [len(model.observable_patterns)])
    yC = numpy.zeros(list(sobmtxC.shape[:2]) + [len(model.observable_patterns)]) # matrix is of shape (nparam, nsamples)

    # specify that this is normalized data
    if norm is True:
        # First process the A and B matrices
        print "processing matrix A, %d iterations:", sobmtxA.shape[0]
        for i in range(sobmtxA.shape[0]):
            outlist = odesolve(model, time, envlist, sobmtxA[i], useparams, ic)
            datamax = numpy.max(outlist[0], axis = 1)
            datamin = numpy.min(outlist[0], axis = 1)
            outlistnorm = ((outlist[0].T - datamin)/(datamax-datamin)).T
            outlistnorm[0] = outlist[0][0].copy() # xpdata[0] replace time from original array
            yA[i] = compare_data(xpdata, outlistnorm, xspairlist, vardata)
            spinner(i)

        print "\nprocessing matrix B, %d iterations:", sobmtxB.shape[0]
        for i in range(sobmtxB.shape[0]):
            outlist = odesolve(model, time, envlist, sobmtxB[i], useparams, ic)
            datamax = numpy.max(outlist[0], axis = 1)
            datamin = numpy.min(outlist[0], axis = 1)
            outlistnorm = ((outlist[0].T - datamin)/(datamax-datamin)).T
            # xpdata[0] should be time, get from original array
            outlistnorm[0] = outlist[0][0].copy()
            yB[i] = compare_data(xpdata, outlistnorm, xspairlist, vardata)
            spinner(i)

        # now the C matrix, a bit more complicated b/c it is of size params x samples
        print "\nprocessing matrix C_n, %d parameters:"%(sobmtxC.shape[0])
        for i in range(sobmtxC.shape[0]):
            print "\nprocessing processing parameter %d, %d iterations"%(i,sobmtxC.shape[1])
            for j in range(sobmtxC.shape[1]):
                outlist = odesolve(model, time, envlist, sobmtxC[i][j], useparams, ic)
                datamax = numpy.max(outlist[0], axis = 1)
                datamin = numpy.min(outlist[0], axis = 1)
                outlistnorm = ((outlist[0].T - datamin)/(datamax-datamin)).T
                # xpdata[0] should be time, get from original array
                outlistnorm[0] = outlist[0][0].copy()
                yC[i][j] = compare_data(xpdata, outlistnorm, xspairlist, vardata)
                spinner(j)
    else:
        # First process the A and B matrices
        print "processing matrix A:"
        for i in range(sobmtxA.shape[0]):
            outlist = odesolve(model, time, envlist, sobmtxA[i], useparams, ic)
            yA[i] = compare_data(xpdata, outlist[0], xspairlist, vardata)
            spinner(i)

        print "processing matrix B:"
        for i in range(sobmtxB.shape[0]):
            outlist = odesolve(model, time, envlist, sobmtxB[i], useparams, ic)
            yB[i] = compare_data(xpdata, outlistnorm, xspairlist, vardata)
            spinner(i)

        print "processing matrix C_n"
        for i in range(sobmtxC.shape[0]):
            print "processing processing parameter %d"%i
            for j in range(sobmtxC.shape[1]):
                outlist = odesolve(model, time, envlist, sobmtxC[i][j], useparams, ic)
                yC[i][j] = compare_data(xpdata, outlistnorm, xspairlist, vardata)
                spinner(j)

    if fileobj:
        if norm:
            writetofile(fileobj, params, outlistnorm, objout)
        else:
            writetofile(fileobj, params, outlist, objout)
    
    return yA, yB, yC

def getvarsens(yA, yB, yC):
    """Calculate the array of S_i and ST_i for each parameter given yA, yB, yC matrices
    from the multi-sampling runs. Calculate S_i and ST_i as follows:
    
    Parameter sensitivity:
    ----------------------
            U_j - E^2 
    S_j = ------------
               V(y)
 
    U_j = 1/n \sum yA * yC_j

    E^2 = 1/n \sum yA * 1/n \sum yB

    Total effect sensitivity (i.e. non additive part):
    --------------------------------------------------
                  U_-j - E^2
     ST_j = 1 - -------------
                      V(y)

    U_-j = 1/n \sum yB * yC_j

    E^2 = { 1/n \sum yB * yB }^2


    In both cases, calculate V(y) from yA and yB


    """
    nparms = yC.shape[0] # should be the number of parameters
    nsamples = yC.shape[1] # should be the number of samples from the original matrix
    nobs = yC.shape[-1]    # the number of observables (this is linked to BNG usage, generalize?)

    #first get V(y) from yA and yB

    varyA = numpy.var(yA, axis=0, ddof=1)
    varyB = numpy.var(yB, axis=0, ddof=1)

    # now get the E^2 values for the S and ST calculations
    E_s  = numpy.average((yA * yB), axis=0)
    E_st = numpy.average(yB, axis=0) ** 2

    #allocate the S_i and ST_i arrays
    Sens = numpy.zeros((nparms,nobs))
    SensT = numpy.zeros((nparms,nobs))

    # now get the U_j and U_-j values and store them 
    for i in range(nparms):
        Sens[i]  =        (((yA * yC[i]).sum(axis=0)/(nsamples-1.)) - E_s ) / varyA
        SensT[i] = 1.0 - ((((yB * yC[i]).sum(axis=0)/(nsamples-1.)) - E_st) / varyB)

    return Sens, SensT
        

def writetofile(fout, simparms, simdata, temperature):
    imax, jmax = simdata.shape
    nparms = len(simparms)

    fout.write('# TEMPERATURE\n{}\n'.format(temperature))
    fout.write('# PARAMETERS ({})\n'.format(len(simparms)))
    for i in range(nparms):
        fout.write('{}'.format(simparms[i]))
        if (i !=0 and i%5 == 0) or (i == nparms-1):
            fout.write('\n')
        else:
            fout.write(', ')
            
    fout.write('# SIMDATA ({},{})\n'.format(imax, jmax))
    for i in range(imax):
        fout.write('# {}\n'.format(i))
        for j in range(jmax):
            fout.write('{}'.format(simdata[i][j]))
            if (j != 0 and j%10 == 0) or (j == jmax-1):
                fout.write('\n')
            else:
                fout.write(', ')
    fout.write('#-------------------------------------------------------------------------------------------------\n')
    return

def extract_initial_params(model):
    return dict([(x.name,x.value) for x in model.parameters_initial_conditions().values()])

def extract_all_params(model):
    return dict(zip(model.parameters.keys(), [x.value for x in model.parameters.values()]))

def set_initial_params(model, params):
    for i in range(len(model.initial_conditions)):
        for j in range(len(model.initial_conditions[i])):
            if type(model.initial_conditions[i][j])==pysb.core.Parameter and \
                    model.initial_conditions[i][j].name in params.keys():
                model.initial_conditions[i][j].value = \
                    params[model.initial_conditions[i][j].name]
    return model

def set_parameters(model, params):
    for p in params.keys():
        model.parameters[p].value = params[p]
    return model

def sample(model,n,tfinal=30000,nsteps=1000):
    params = extract_initial_params(model)
    param_names = params.keys()

    numparam = len(params)
    sobgen = QuantLib.SobolRsg(numparam, 64)
    sobvals = [sobgen.nextSequence().value() for i in range(n)]
    # map the sobol values to Gaussian using R's inverse CDF
    # and multiply them
    sobvals_scaled = getgauss(sobvals,params.values(),0.125)

    finalquants = numpy.zeros((n,len(model.observables)))

    for i in range(n):
        # set parameters
        s = sobvals_scaled[i]
        for j in range(len(s)):
            params[param_names[j]] = s[j]
        model = set_initial_params(model, params)
        # sample from model
#        [envlist,modelparms] = odeinit(model,nsteps=nsteps)
#        [xyobs,xout,yout,yobs] = odesolve(model,tfinal,envlist,modelparms)
#        finalquants[i] = [y[nsteps - 1] for y in yobs]
        t = numpy.linspace(0,tfinal,nsteps)
        yout = pysb.integrate.odesolve(model, t)
        finalquants[i] = [yout[k][nsteps - 1] for k in model.observables.keys()]

    return finalquants

def sample_vary_all(model,n,knockouts=3,tfinal=30000,nsteps=1000):
    params = extract_all_params(model)
    param_names = params.keys()

    numparam = len(params)
    sobgen = QuantLib.SobolRsg(numparam, 64)
    sobvals = [sobgen.nextSequence().value() for i in range(n)]
    # map the sobol values to Gaussian using R's inverse CDF
    # and multiply them
    sobvals_scaled = getgauss(sobvals,params.values(),0.125)
    
#    finalquants = numpy.zeros((n,len(model.observables)))
    t = numpy.linspace(0,tfinal,nsteps)

    # open files
    pfile = open('params.csv','wb')
    ofile = open('observations.csv','wb')

    # write headers
    import csv
    pcsv = csv.writer(pfile)
    ocsv = csv.writer(ofile)
    pcsv.writerow(params.keys())
    ocsv.writerow(model.observables.keys())

    for i in range(n):
        # set parameters
        s = sobvals_scaled[i]
        for j in range(len(s)):
            params[param_names[j]] = s[j]
        # do knockouts
        knockout_params = random.sample(params.keys(),knockouts)
        for k in knockout_params:
            params[k] = 0
        model = set_parameters(model, params)
        pcsv.writerow(params.values())
        # sample from model
        yout = integrate.odesolve(model, t)
        ocsv.writerow([yout[k][nsteps - 1] for k in model.observables.keys()])
#        finalquants[i] = [yout[k][nsteps - 1] for k in model.observables.keys()]

    pfile.close()
    ofile.close()

#    return finalquants

if __name__ == "__main__":
     model = simple_egfr.model
     smp = sample(model,10)
     numpy.savetxt('gabi_sampling_test.csv', smp, delimiter=',')
